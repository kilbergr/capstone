# tests to make sure our source repo is consistent, and no build commands need to be run
import os
import subprocess
from pathlib import Path
import pytest


@pytest.mark.django_db(databases=['default', 'capdb', 'user_data'])
def test_makemigrations():
    # this can't be done as
    #   management.call_command('makemigrations', dry_run=True, stdout=out)
    # because no migrations are reported given the test db settings,
    # so shell out to the real makemigrations
    out = subprocess.run(
        ['python', 'manage.py', 'makemigrations', '--dry-run'],
        capture_output=True,
        check=True
    ).stdout.decode('utf8')
    assert out == 'No changes detected\n', "Model changes detected. Please run ./manage.py makemigrations"

def test_pip_compile():
    existing_requirements = Path('requirements.txt').read_bytes()
    subprocess.check_call(["fab", "pip-compile"], stdout=subprocess.PIPE,
                          # strip COV_ environment variables so pip-compile doesn't try to report test coverage
                          env={k:v for k,v in os.environ.items() if not k.startswith('COV_')})
    new_requirements = Path('requirements.txt').read_bytes()
    assert new_requirements == existing_requirements, "Changes detected to requirements.in. Please run fab pip-compile"

def test_flake8():
    subprocess.check_call('flake8')
