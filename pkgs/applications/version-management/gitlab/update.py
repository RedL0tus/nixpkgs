#!/usr/bin/env nix-shell
#! nix-shell -i python3 -p bundix bundler common-updater-scripts nix nix-prefetch-git python3 python3Packages.requests python3Packages.click python3Packages.click-log vgo2nix yarn2nix

import click
import click_log
import os
import re
import logging
import subprocess
import json
import pathlib
from distutils.version import LooseVersion
from typing import Iterable

import requests

logger = logging.getLogger(__name__)


class GitLabRepo:
    version_regex = re.compile(r"^v\d+\.\d+\.\d+(\-rc\d+)?(\-ee)?")
    def __init__(self, owner: str = 'gitlab-org', repo: str = 'gitlab-foss'):
        self.owner = owner
        self.repo = repo

    @property
    def url(self):
        return f"https://gitlab.com/{self.owner}/{self.repo}"

    @property
    def tags(self) -> Iterable[str]:
        r = requests.get(self.url + "/refs?sort=updated_desc&ref=master").json()
        tags = r.get("Tags", [])

        # filter out versions not matching version_regex
        versions = list(filter(self.version_regex.match, tags))

        # sort, but ignore v and -ee for sorting comparisons
        versions.sort(key=lambda x: LooseVersion(x.replace("v", "").replace("-ee", "")), reverse=True)
        return versions

    def get_git_hash(self, rev: str):
        out = subprocess.check_output(['nix-prefetch-git', self.url, rev])
        j = json.loads(out)
        return j['sha256']

    @staticmethod
    def rev2version(tag: str) -> str:
        """
        normalize a tag to a version number.
        This obviously isn't very smart if we don't pass something that looks like a tag
        :param tag: the tag to normalize
        :return: a normalized version number
        """
        # strip v prefix
        version = re.sub(r"^v", '', tag)
        # strip -ee suffix
        return re.sub(r"-ee$", '', version)

    def get_file(self, filepath, rev):
        """
        returns file contents at a given rev
        :param filepath: the path to the file, relative to the repo root
        :param rev: the rev to fetch at
        :return:
        """
        return requests.get(self.url + f"/raw/{rev}/{filepath}").text

    def get_data(self, rev):
        version = self.rev2version(rev)

        passthru = {v: self.get_file(v, rev).strip() for v in ['GITALY_SERVER_VERSION', 'GITLAB_PAGES_VERSION',
                                                               'GITLAB_SHELL_VERSION', 'GITLAB_WORKHORSE_VERSION']}
        return dict(version=self.rev2version(rev),
                    repo_hash=self.get_git_hash(rev),
                    owner=self.owner,
                    repo=self.repo,
                    rev=rev,
                    passthru=passthru)


def _get_data_json():
    data_file_path = pathlib.Path(__file__).parent / 'data.json'
    with open(data_file_path, 'r') as f:
        return json.load(f)


def _call_update_source_version(pkg, version):
    """calls update-source-version from nixpkgs root dir"""
    nixpkgs_path = pathlib.Path(__file__).parent / '../../../../'
    return subprocess.check_output(['update-source-version', pkg, version], cwd=nixpkgs_path)


@click_log.simple_verbosity_option(logger)
@click.group()
def cli():
    pass


@cli.command('update-data')
@click.option('--rev', default='latest', help='The rev to use (vX.Y.Z[-ee]), or \'latest\'')
@click.option('--ee', type=bool, default=False, help='Use GitLab EE instead of GitLab CE')
def update_data(rev: str, ee: bool):
    """Update data.nix"""
    repo_name = 'gitlab' if ee else 'gitlab-foss'
    repo = GitLabRepo(repo=repo_name)

    if rev == 'latest':
        # filter out pre and re releases
        rev = next(filter(lambda x: not ('rc' in x or x.endswith('pre')), repo.tags))
    logger.debug(f"Using rev {rev}")

    version = repo.rev2version(rev)
    logger.debug(f"Using version {version}")

    data_file_path = pathlib.Path(__file__).parent / 'data.json'

    data = repo.get_data(rev)

    with open(data_file_path.as_posix(), 'w') as f:
        json.dump(data, f, indent=2)


@cli.command('update-rubyenv')
@click.option('--ee', type=bool, default=False, help='Use GitLab EE instead of GitLab CE')
def update_rubyenv(ee: bool):
    """Update rubyEnv"""
    repo_name = 'gitlab' if ee else 'gitlab-foss'
    repo = GitLabRepo(repo=repo_name)
    rubyenv_dir = pathlib.Path(__file__).parent / f"rubyEnv"

    # load rev from data.json
    data = _get_data_json()
    rev = data['rev']

    for fn in ['Gemfile.lock', 'Gemfile']:
        with open(rubyenv_dir / fn, 'w') as f:
            f.write(repo.get_file(fn, rev))

    subprocess.check_output(['bundle', 'lock'], cwd=rubyenv_dir)
    subprocess.check_output(['bundix'], cwd=rubyenv_dir)


@cli.command('update-yarnpkgs')
@click.option('--ee', type=bool, default=False, help='Use GitLab EE instead of GitLab CE')
def update_yarnpkgs(ee: bool):
    """Update yarnPkgs"""
    repo_name = 'gitlab' if ee else 'gitlab-foss'
    repo = GitLabRepo(repo=repo_name)
    yarnpkgs_dir = pathlib.Path(__file__).parent

    # load rev from data.json
    data = _get_data_json()
    rev = data['rev']

    with open(yarnpkgs_dir / 'yarn.lock', 'w') as f:
        f.write(repo.get_file('yarn.lock', rev))

    with open(yarnpkgs_dir / 'yarnPkgs.nix', 'w') as f:
        subprocess.run(['yarn2nix'], cwd=yarnpkgs_dir, check=True, stdout=f)

    os.unlink(yarnpkgs_dir / 'yarn.lock')


@cli.command('update-gitaly')
def update_gitaly():
    """Update gitaly"""
    data = _get_data_json()
    gitaly_server_version = data['passthru']['GITALY_SERVER_VERSION']
    repo = GitLabRepo(repo='gitaly')
    gitaly_dir = pathlib.Path(__file__).parent / 'gitaly'

    for fn in ['Gemfile.lock', 'Gemfile']:
        with open(gitaly_dir / fn, 'w') as f:
            f.write(repo.get_file(f"ruby/{fn}", f"v{gitaly_server_version}"))

    for fn in ['go.mod', 'go.sum']:
        with open(gitaly_dir / fn, 'w') as f:
            f.write(repo.get_file(fn, f"v{gitaly_server_version}"))

    subprocess.check_output(['bundle', 'lock'], cwd=gitaly_dir)
    subprocess.check_output(['bundix'], cwd=gitaly_dir)

    os.environ['GOROOT'] = ""
    subprocess.check_output(['vgo2nix', '--keep-going'], cwd=gitaly_dir)

    for fn in ['go.mod', 'go.sum']:
        os.unlink(gitaly_dir / fn)

    _call_update_source_version('gitaly', gitaly_server_version)


@cli.command('update-gitlab-shell')
def update_gitlab_shell():
    """Update gitlab-shell"""
    data = _get_data_json()
    gitlab_shell_version = data['passthru']['GITLAB_SHELL_VERSION']
    _call_update_source_version('gitlab-shell', gitlab_shell_version)

    repo = GitLabRepo(repo='gitlab-shell')
    gitlab_shell_dir = pathlib.Path(__file__).parent / 'gitlab-shell'

    for fn in ['go.mod', 'go.sum']:
        with open(gitlab_shell_dir / fn, 'w') as f:
            f.write(repo.get_file(fn, f"v{gitlab_shell_version}"))

    os.environ['GOROOT'] = ""
    subprocess.check_output(['vgo2nix', '--keep-going'], cwd=gitlab_shell_dir)

    for fn in ['go.mod', 'go.sum']:
        os.unlink(gitlab_shell_dir / fn)


@cli.command('update-gitlab-workhorse')
def update_gitlab_workhorse():
    """Update gitlab-workhorse"""
    data = _get_data_json()
    gitlab_workhorse_version = data['passthru']['GITLAB_WORKHORSE_VERSION']
    _call_update_source_version('gitlab-workhorse', gitlab_workhorse_version)

    repo = GitLabRepo('gitlab-org', 'gitlab-workhorse')
    gitlab_workhorse_dir = pathlib.Path(__file__).parent / 'gitlab-workhorse'

    for fn in ['go.mod', 'go.sum']:
        with open(gitlab_workhorse_dir / fn, 'w') as f:
            f.write(repo.get_file(fn, f"v{gitlab_workhorse_version}"))

    os.environ['GOROOT'] = ""
    subprocess.check_output(['vgo2nix', '--keep-going'], cwd=gitlab_workhorse_dir)

    for fn in ['go.mod', 'go.sum']:
        os.unlink(gitlab_workhorse_dir / fn)

@cli.command('update-all')
@click.option('--rev', default='latest', help='The rev to use (vX.Y.Z[-ee]), or \'latest\'')
@click.option('--ee', type=bool, default=False, help='Use GitLab EE instead of GitLab CE')
@click.pass_context
def update_all(ctx, rev: str, ee: bool):
    """Update all gitlab components to the latest stable release"""
    ctx.invoke(update_data, rev=rev, ee=ee)
    ctx.invoke(update_rubyenv, ee=ee)
    ctx.invoke(update_yarnpkgs, ee=ee)
    ctx.invoke(update_gitaly)
    ctx.invoke(update_gitlab_shell)
    ctx.invoke(update_gitlab_workhorse)


if __name__ == '__main__':
    cli()
