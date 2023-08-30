import base64
import boto3
import hashlib
import json
import requests
from botocore.exceptions import ClientError
from collections import namedtuple
from celery import group, shared_task

from capapi.documents import CaseDocument
from capapi.serializers import (
    ConvertNoLoginCaseDocumentSerializer,
)
from capdb.models import Reporter

s3_client = boto3.client("s3")
api_endpoint = "https://api.case.law/v1/"


def put_reporters_on_s3_trial(redacted: bool) -> None:
    """
    Kicks off the full cascading S3 file creation series
    for a subsection of reporters.
    """
    # set bucket name for all operations
    bucket = get_bucket_name(redacted)

    current_endpoint = f"{api_endpoint}reporters/"
    print("Converting files from ", current_endpoint)
    response = requests.get(current_endpoint)
    results = response.json()
    reporters_metadata = ""
    all_volumes_metadata = ""

    # write each entry into jsonl
    for result in results["results"]:
        # for each reporter, kick off cascading export to S3
        reporter_metadata, subset_volumes_metadata = export_cases_to_s3(
            bucket, redacted, result["id"]
        )
        reporters_metadata += reporter_metadata
        all_volumes_metadata += subset_volumes_metadata

    # uploads all reporters metadata to top level
    hash_and_upload(
        reporters_metadata,
        bucket,
        "ReportersMetadata.jsonl",
        "application/jsonl",
    )

    # uploads all volumes metadata to top level
    hash_and_upload(
        all_volumes_metadata,
        bucket,
        "VolumesMetadata.jsonl",
        "application/jsonl",
    )


def put_reporters_on_s3(redacted: bool) -> None:
    """
    Kicks off the full cascading file creation series.
    """
    # set bucket name for all operations
    bucket = get_bucket_name(redacted)

    current_endpoint = f"{api_endpoint}reporters/"
    previous_cursor = None
    current_cursor = ""
    reporters_metadata = ""
    all_volumes_metadata = ""

    while current_endpoint:
        print("Converting files from ", current_endpoint)
        response = requests.get(current_endpoint)
        results = response.json()

        # write each entry into jsonl
        for result in results["results"]:
            # for each reporter, kick off cascading export to S3
            reporter_metadata, subset_volumes_metadata = export_cases_to_s3(
                bucket, redacted, result["id"]
            )
            reporters_metadata += reporter_metadata
            all_volumes_metadata += subset_volumes_metadata

        # update cursor to access next endpoint
        current_cursor = results["next"]
        if current_cursor != previous_cursor:
            print("Update next to: ", current_cursor)

        previous_cursor = current_cursor
        current_endpoint = current_cursor

    # uploads all reporters metadata to top level
    hash_and_upload(
        reporters_metadata,
        bucket,
        "ReportersMetadata.jsonl",
        "application/jsonl",
    )

    # uploads all volumes metadata to top level
    hash_and_upload(
        all_volumes_metadata,
        bucket,
        "VolumesMetadata.jsonl",
        "application/jsonl",
    )


def export_cases_to_s3(bucket: str, redacted: bool, reporter_id: str) -> tuple:
    """
    Write .jsonl file with all cases per reporter.
    """
    reporter = Reporter.objects.get(pk=reporter_id)

    # Make sure there are volumes in the reporter
    if not reporter.volumes.exclude(out_of_scope=True):
        print("WARNING: Reporter '{}' contains NO VOLUMES.".format(reporter.full_name))
        # Returning empty string to have something to append to reporter metadata
        return ("", "")

    # Make sure there are cases in the reporter
    cases_search = CaseDocument.raw_search().filter("term", reporter__id=reporter.id)
    if cases_search.count() == 0:
        print("WARNING: Reporter '{}' contains NO CASES.".format(reporter.full_name))
        # Returning empty string to have something to append to reporter metadata
        return ("", "")

    # TODO: address reporters that share slug
    if reporter_id in reporter_slug_dict:
        reporter_prefix = reporter_slug_dict[reporter_id]
    else:
        reporter_prefix = reporter.short_name_slug

    # upload reporter metadata
    reporter_metadata = put_reporter_metadata(bucket, reporter, reporter_prefix)

    # get in-scope volumes with volume numbers in each reporter
    subset_volumes_metadata = ""

    volumes_metadata = group(
        export_cases_by_volume.s(
            volume=volume,
            reporter_prefix=reporter_prefix,
            dest_bucket=bucket,
            redacted=redacted,
        )
        for volume in (
            reporter.volumes.exclude(volume_number=None)
            .exclude(volume_number="")
            .exclude(out_of_scope=True)
        )
    )()

    for volume_metadata in volumes_metadata.get():
        subset_volumes_metadata += volume_metadata

    return (reporter_metadata, subset_volumes_metadata)


@shared_task
def export_cases_by_volume(
    volume: object, reporter_prefix: str, dest_bucket: str, redacted: bool
) -> str:
    """
    Write a .json file for each case per volume.
    Write a .jsonl file with all cases' metadata per volume.
    Write a .jsonl file with all volume metadata for this collection.
    """

    case_file_name_index = 1
    prev_case_first_page = None

    vars = {
        "serializer": ConvertNoLoginCaseDocumentSerializer,
        "query_params": {"body_format": "text"},
    }

    cases = list(volume.case_metadatas.select_related().order_by("case_id"))

    if len(cases) == 0:
        print("WARNING: Volume '{}' contains NO CASES.".format(volume.barcode))
        # Returning empty string to have something to append to volume metadata
        return ""

    # open each volume and put case text or metadata into file based on format
    cases_search = CaseDocument.raw_search().filter(
        "term", volume__barcode=volume.barcode
    )

    # create a dictionary to grab data from each CaseDocument search object
    cases_search_by_id = {
        case_search["_source"]["id"]: case_search for case_search in cases_search.scan()
    }

    volume_prefix = f"{reporter_prefix}/{volume.volume_number}"
    volume_metadata = put_volume_metadata(dest_bucket, volume, volume_prefix)

    cases_key = f"{volume_prefix}/Cases/"

    # fetch existing files to compare to what we have
    s3_contents_hashes = fetch_s3_files(dest_bucket, cases_key)

    # fake Request object used for serializing case with DRF's serializer
    vars["fake_request"] = namedtuple("Request", ["query_params", "accepted_renderer"])(
        query_params=vars["query_params"],
        accepted_renderer=None,
    )
    # fake Request object used for serializing cases with DRF's serializer
    vars["fake_request"] = namedtuple("Request", ["query_params", "accepted_renderer"])(
        query_params={"body_format": "text"},
        accepted_renderer=None,
    )

    # create a metadata contents string to append case metadata content
    metadata_contents = ""

    # store the serialized case data
    for case in cases:
        # identify associated search item to add additional data
        item = cases_search_by_id[case.id]

        serializer = vars["serializer"](
            item["_source"],
            context={
                "request": vars["fake_request"],
                "first_page_order": case.first_page_order,
                "last_page_order": case.last_page_order,
            },
        )

        # add data to metadata_contents string without 'casebody'
        metadata_data = serializer.data
        metadata_data.pop("casebody", None)
        metadata_contents += json.dumps(metadata_data) + "\n"

        # compose each casefile with a hash
        case_contents = json.dumps(serializer.data) + "\n"
        hash_object = hashlib.sha256(case_contents.encode("utf-8"))
        case_contents_hash = base64.b64encode(hash_object.digest()).decode()

        # calculate casefile name
        if prev_case_first_page == case.first_page:
            case_file_name_index += 1
        else:
            case_file_name_index = 1
        case_file_name = (
            f"{case.first_page.zfill(4)}-{str(case_file_name_index).zfill(2)}.json"
        )

        # set so we can use to determine multiple cases on single page
        prev_case_first_page = case.first_page

        # identify key: hash pair for current case
        dest_key = f"{cases_key}{case_file_name}"
        s3_key_hash = s3_contents_hashes.pop(dest_key, None)

        if s3_key_hash is None or s3_key_hash != case_contents_hash:
            hash_and_upload(
                case_contents,
                dest_bucket,
                dest_key,
                "application/jsonl",
            )

    # remove files from S3 that would otherwise create repeats
    for s3_case_key in s3_contents_hashes:
        s3_client.delete_object(
            Bucket=dest_bucket,
            Key=s3_case_key,
        )

    hash_and_upload(
        metadata_contents,
        dest_bucket,
        f"{volume_prefix}/CasesMetadata.jsonl",
        "application/jsonl",
    )

    # copies each volume PDF to new location if it doesn't already exist
    copy_volume_pdf(volume, volume_prefix, dest_bucket, redacted)
    # return metadata for single volume
    return volume_metadata


# Reporter-specific helper functions

# Some reporters share a slug, so we have to differentiate with ids
reporter_slug_dict = {
    "415": "us-ct-cl",
    "657": "wv-ct-cl",
    "580": "mass-app-div-annual",
    "576": "mass-app-div",
}


def put_reporter_metadata(bucket: str, reporter: object, key: str) -> str:
    """
    Write a .json file with just the reporter metadata.
    Return the line of reporter metadata to be used in all reporters metadata file.
    """
    response = requests.get(f"{api_endpoint}reporters/{reporter.id}/")
    results = response.json()

    # add additional fields from reporter obj
    results["harvard_hollis_id"] = reporter.hollis

    # remove unnecessary fields
    results.pop("url", None)
    results.pop("frontend_url", None)
    try:
        for jurisdiction in results["jurisdictions"]:
            jurisdiction.pop("slug", None)
            jurisdiction.pop("whitelisted", None)
            jurisdiction.pop("url", None)
    except KeyError as err:
        print(f"Cannot pop field {err} because 'jurisdictions' doesn't exist")

    reporter_metadata = json.dumps(results) + "\n"
    # add each line to reporters_metadata string
    hash_and_upload(
        reporter_metadata, bucket, f"{key}/ReporterMetadata.json", "application/json"
    )
    return reporter_metadata


# Volume-specific helper functions


def put_volume_metadata(bucket: str, volume: object, key: str) -> str:
    """
    Write a .json file with just the single volume metadata.
    """
    response = requests.get(f"{api_endpoint}volumes/{volume.barcode}/")
    results = response.json()
    # change "barcode" key to "id" key
    results["id"] = results.pop("barcode", None)

    # add additional fields from model
    results["harvard_hollis_id"] = volume.hollis_number
    results["spine_start_year"] = volume.spine_start_year
    results["spine_end_year"] = volume.spine_end_year
    results["publication_city"] = volume.publication_city
    results["second_part_of_id"] = volume.second_part_of_id

    # add information about volume's nominative_reporter
    if volume.nominative_reporter_id:
        results["nominative_reporter"] = {}
        results["nominative_reporter"]["id"] = volume.nominative_reporter_id
        results["nominative_reporter"][
            "short_name"
        ] = volume.nominative_reporter.short_name
        results["nominative_reporter"][
            "full_name"
        ] = volume.nominative_reporter.full_name
        results["nominative_reporter"][
            "volume_number"
        ] = volume.nominative_volume_number
        results.pop("nominative_volume_number", None)
        results.pop("nominative_name", None)
    elif volume.nominative_reporter_id is None and (
        volume.nominative_volume_number or volume.nominative_name
    ):
        results["nominative_reporter"] = {}
        results["nominative_reporter"][
            "volume_number"
        ] = volume.nominative_volume_number
        results["nominative_reporter"]["nominative_name"] = volume.nominative_name
    else:
        results["nominative_reporter"] = None

    # remove unnecessary fields
    results.pop("reporter", None)
    results.pop("reporter_url", None)
    results.pop("url", None)
    results.pop("pdf_url", None)
    results.pop("frontend_url", None)
    try:
        for jurisdiction in results["jurisdictions"]:
            jurisdiction.pop("slug", None)
            jurisdiction.pop("whitelisted", None)
            jurisdiction.pop("url", None)
    except KeyError as err:
        print(f"Cannot pop field {err} because 'jurisdictions' doesn't exist")

    volume_metadata = json.dumps(results) + "\n"
    hash_and_upload(
        volume_metadata, bucket, f"{key}/VolumeMetadata.json", "application/json"
    )
    return volume_metadata


def copy_volume_pdf(
    volume: object, volume_prefix: str, dest_bucket: str, redacted: bool
) -> None:
    """
    Copy PDF volume from original location to destination bucket
    """
    if redacted:
        source_prefix = "pdf/redacted"
    else:
        source_prefix = "pdf/unredacted"

    try:
        s3_client.head_object(Bucket=dest_bucket, Key=f"{volume_prefix}/Volume.pdf")
        print(f"{dest_bucket}/{volume_prefix}/Volume.pdf already uploaded!")
    except ClientError as err:
        if err.response["Error"]["Code"] == "404":
            # "With a copy command, the checksum of the object is a direct checksum of the full object."
            # https://docs.aws.amazon.com/AmazonS3/latest/userguide/checking-object-integrity.html
            copy_source = {
                "Bucket": "harvard-cap-archive",
                "Key": f"{source_prefix}/{volume.barcode}.pdf",
            }
            copy_object_params = {
                "Bucket": dest_bucket,
                "Key": f"{volume_prefix}/Volume.pdf",
                "CopySource": copy_source,
            }

            s3_client.copy_object(**copy_object_params)
            print(
                f"Copied {source_prefix}/{volume.barcode}.pdf to \
                {volume_prefix}/Volume.pdf"
            )
        else:
            raise Exception(
                f"Cannot upload {source_prefix}/{volume.barcode}.pdf to \
                {volume_prefix}/Volume.pdf: %s"
                % err
            )


# Case-specific helper functions


def fetch_s3_files(bucket: str, key: str) -> dict:
    """
    Return a dictionary of bucket contents format key: hash
    """
    try:
        s3_contents_hash = {}
        response = s3_client.list_objects_v2(Bucket=bucket, Prefix=key)
    except ClientError as err:
        raise Exception(f"Cannot list objects {bucket}/{key}: %s" % err)
    if "Contents" not in response:
        return s3_contents_hash
    else:
        for case in response["Contents"]:
            # Get the object's metadata
            try:
                response = s3_client.get_object_attributes(
                    Bucket=bucket, Key=case["Key"], ObjectAttributes=["Checksum"]
                )

                existing_hash = response.get("Checksum", {}).get("ChecksumSHA256")
                s3_contents_hash[case["Key"]] = existing_hash
            except ClientError as err:
                raise Exception(f"Cannot check file {bucket}/{case['Key']}: %s" % err)

    return s3_contents_hash


# General helper functions


def hash_and_upload(contents: str, bucket: str, key: str, content_type: str) -> None:
    """
    Hash created file and upload to S3
    """
    # Calculate the SHA256 hash of the contents data
    hash_object = hashlib.sha256(contents.encode("utf-8"))
    sha256_hash = base64.b64encode(hash_object.digest()).decode()
    # upload file to S3
    try:
        s3_client.put_object(
            Body=contents,
            Bucket=bucket,
            Key=key,
            ContentType=content_type,
            ChecksumSHA256=sha256_hash,
        )
        print(f"Completed {key}")
    except ClientError as err:
        raise Exception(f"Error uploading {key}: %s" % err)


def get_bucket_name(redacted: bool) -> str:
    """
    Create bucket name based on redaction status
    """
    if redacted:
        bucket = "cap-redacted"
    else:
        bucket = "cap-unredacted"
    return bucket
