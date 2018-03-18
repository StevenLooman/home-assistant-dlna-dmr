import sys

import os.path

from setuptools import setup
from setuptools import find_packages
from setuptools.command.test import test as TestCommand


class PyTest(TestCommand):
    def finalize_options(self):
        TestCommand.finalize_options(self)
        self.test_args = [
            '--strict',
            '--verbose',
            '--tb=long',
            'tests']
        self.test_suite = True

    def run_tests(self):
        import pytest
        errcode = pytest.main(self.test_args)
        sys.exit(errcode)


here = os.path.abspath(os.path.dirname(__file__))
with open(os.path.join(here, 'README.md'), encoding='utf-8') as f:
    LONG_DESCRIPTION = f.read()


INSTALL_REQUIRES=[
]


TEST_REQUIRES=[
    'pytest',
    'pytest-asyncio',
]


setup(
    name='home-assistant-dlna-dmr',
    version='0.1.0',
    description='DLNA/DMR component from Home Assistant',
    long_description=LONG_DESCRIPTION,
    url='https://github.com/StevenLooman/home-assistant-dlna-dmr',
    author='Steven Looman',
    author_email='steven.looman@gmail.com',
    packages=['home_assistant_dlna_dmr'],
    install_requires=INSTALL_REQUIRES,
    tests_require=TEST_REQUIRES,
    cmdclass={'test': PyTest},
)
