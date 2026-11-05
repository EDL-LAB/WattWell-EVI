#!/usr/bin/python3
"""Install the official ARM GitHub CLI without APT."""
import hashlib
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile

import configure

VERSION = '2.103.3'
ARCHIVE_NAME = 'gh_' + VERSION + '_linux_armv6'
ARCHIVE_SHA256 = '57f56848b069cad21f4fa72f2cc38c2ac01f5f69f443159213a42183a73c89ea'
URL = 'https://github.com/cli/cli/releases/download/v' + VERSION + '/' + ARCHIVE_NAME + '.tar.gz'


def main():
    if sys.argv[1:]:
        raise RuntimeError('Usage: python3 scripts/install-gh.py')
    if configure.run(['dpkg', '--print-architecture']).stdout.strip() != 'armhf':
        raise RuntimeError('This installer supports only armhf.')
    target = Path('/usr/local/bin/gh') if os.geteuid() == 0 else Path.home() / '.local/bin/gh'
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise RuntimeError('The destination is not a regular file: ' + str(target))
    target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.gh-install-', dir=str(target.parent)) as temporary:
        directory = Path(temporary)
        archive = directory / 'gh.tar.gz'
        print('Downloading official GitHub CLI ' + VERSION + ' for ARM; verifying SHA-256...', flush=True)
        configure.download(configure.downloader(), URL, archive, ARCHIVE_SHA256)
        candidate = directory / 'gh'
        with tarfile.open(str(archive), 'r:gz') as package:
            members = [member for member in package.getmembers() if member.name == ARCHIVE_NAME + '/bin/gh']
            if len(members) != 1 or not members[0].isfile() or not 0 < members[0].size <= 100 * 1024 * 1024:
                raise RuntimeError('The archive does not contain the expected regular gh binary.')
            with package.extractfile(members[0]) as source, candidate.open('wb') as output:
                shutil.copyfileobj(source, output)
        candidate.chmod(0o755)
        version = configure.run([str(candidate), '--version']).stdout
        if not version.startswith('gh version ' + VERSION + ' '):
            raise RuntimeError('The downloaded binary did not report the expected version.')
        if target.exists():
            if hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(candidate.read_bytes()).digest():
                raise RuntimeError('A different gh already exists; it was not overwritten: ' + str(target))
            target.chmod(0o755)
        else:
            os.link(str(candidate), str(target))
    print('GitHub CLI is installed at ' + str(target))
    print('GitHub CLI installation did not change APT, system packages or services.')
    if str(target.parent) not in os.environ.get('PATH', '').split(os.pathsep):
        directory = '/usr/local/bin' if os.geteuid() == 0 else '$HOME/.local/bin'
        print('For this shell: export PATH="' + directory + ':$PATH"')


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print('ERROR: ' + str(error), file=sys.stderr)
        sys.exit(1)
