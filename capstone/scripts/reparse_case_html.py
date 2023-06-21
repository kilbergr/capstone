from capdb.models import CaseMetadata, CaseStructure, PageStructure


def make_case_dicts(case_id):
    case_dict = {case_id: {}}
    case = CaseMetadata.objects.get(pk=case_id)
    case_structure = CaseStructure.objects.get(metadata=case)
    for page in case_structure.pages.all():
        page = PageStructure.objects.get(pk=page.id)
        for block in page.blocks:
            breakpoint()
            case_dict[case_id][block["id"]] = page.id
    return case_dict
