#!/usr/bin/env python3
import argparse
import re
import tempfile
from concurrent.futures.thread import ThreadPoolExecutor
from os import makedirs
from os.path import dirname, basename
from pprint import pprint
from subprocess import check_output as run, check_call as exe, CalledProcessError, STDOUT
from time import sleep

import requests

parser = argparse.ArgumentParser(description="COPR to GH Release Synchronizer",
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument("--copr-owner-name", dest="owner_name", required=True, type=str,
                    help="COPR owner name")
parser.add_argument("--copr-project-name", dest="project_name", required=True, type=str,
                    help="COPR project name")
parser.add_argument("--copr-package-name", dest="package_name", required=True, type=str,
                    help="COPR package name")
parser.add_argument("--tag-to-version-re", required=False, type=re.compile,
                    help="a regex that matches a tag and whose group[1] contains an rpm version to match")
parser.add_argument("--tag", required=False, type=str,
                    help="specific tag to process")
parser.add_argument("--fetch-tags", required=False, action="store_true", default=False,
                    help="fetch all tags")
parser.add_argument("--clobber-assets", required=False, action="store_true", default=False,
                    help="for releases that already exist do clobber (re-upload) the assets")
parser.add_argument("--no-ignore-epoch", dest="ignore_epoch", required=False, action="store_false", default=True,
                    help="ignore rpm epoch in version matches")
parser.add_argument("--wait-build", required=False, action="store_true", default=True,
                    help="if we see pending or running builds we'll loop waiting for them to complete")


def main():
    args = parser.parse_args()
    owner_name = args.owner_name
    project_name = args.project_name
    package_name = args.package_name
    tag_to_version_re = args.tag_to_version_re
    tag = args.tag
    fetch_tags = args.fetch_tags
    clobber_assets = args.clobber_assets
    ignore_epoch = args.ignore_epoch
    wait_build = args.wait_build
    spin_loop_delay = 30

    with ThreadPoolExecutor(max_workers=10) as tpe:
        with tempfile.TemporaryDirectory() as tmp_dir:
            with (requests.Session() as s):
                def check_url_exists(url):
                    with s.head(url) as r:
                        return r.status_code == 200

                def get_builds():
                    build_metadata = {}
                    retry = True
                    while retry:
                        retry = False
                        with s.get("https://copr.fedorainfracloud.org/api_3/build/list",
                                   params={"ownername": owner_name,
                                           "projectname": project_name,
                                           }) as r:
                            r.raise_for_status()
                            builds = r.json().get("items", [])

                        for build in builds:
                            if build["state"] == "failed":
                                continue

                            if ((build_package_name := build["source_package"]["name"]) and
                                    build_package_name != package_name):
                                continue

                            def get_version():
                                ver = build["source_package"]["version"]
                                if not ver:
                                    return ver
                                if ignore_epoch and ":" in ver:
                                    ver = ver[ver.index(":") + 1:]
                                return ver

                            version = get_version()
                            if build["state"] in ("pending", "importing", "starting", "running") or not build[
                                "ended_on"]:
                                if wait_build:
                                    retry = True
                                    print(
                                        f"found build id {build['id']} package {build_package_name or '<unknown>'} "
                                        f"version {version or '<unknown>'} {build['state']}"
                                        f" - will retry in {spin_loop_delay} seconds")
                                    sleep(spin_loop_delay)
                                    break
                                else:
                                    continue

                            arches = build["chroots"]
                            bm = {
                                "id": build["id"],
                                "dir_id": basename(dirname(build["source_package"]["url"])),
                                "version": version,
                                "ended_on": build["ended_on"],
                                "source_rpm": build["source_package"]["url"],
                                "package_name": build["source_package"]["name"],
                                "repo_url": build["repo_url"]
                            }
                            if not check_url_exists(bm["source_rpm"]):
                                continue

                            version_arches = build_metadata.setdefault(version, {})
                            for arch in arches:
                                existing_bm = version_arches.setdefault(arch, bm)
                                if existing_bm is bm:
                                    continue
                                if bm["ended_on"] > existing_bm["ended_on"]:
                                    version_arches[arch] = bm

                    return build_metadata

                build_metadata = get_builds()

                def get_build_dir_name(build, platform_arch):
                    return f"{build['repo_url']}/{platform_arch}/{build['dir_id']}-{build['package_name']}"

                def get_arch_url(build, platform_arch: str, build_result: dict):
                    platform, arch = platform_arch[:platform_arch.rindex("-")], platform_arch[
                                                                                platform_arch.rindex("-") + 1:]
                    rpm_name = (
                        f"{build_result['epoch'] + ':' if build_result['epoch'] and build_result['epoch'] > 1 else ''}"
                        f"{build_result['name']}-{build_result['version']}-{build_result['release']}.{build_result['arch']}.rpm")
                    return (f"{platform}-{rpm_name}",
                            f"{get_build_dir_name(build, platform_arch)}/{rpm_name}")

                def get_build_results(build, platform_arch: str):
                    results_url = f"{get_build_dir_name(build, platform_arch)}/results.json"
                    with s.get(results_url) as r:
                        if r.status_code == 404:
                            return []
                        else:
                            r.raise_for_status()
                        return r.json()["packages"]

                version_files = {}
                for k, build in build_metadata.items():
                    files = set()
                    for arch in build:
                        arch_bm = build[arch]
                        files.add((basename(arch_bm["source_rpm"]), arch_bm["source_rpm"]))
                        for build_result in get_build_results(arch_bm, arch):
                            if build_result["arch"] == "src":
                                continue
                            file_name, url = get_arch_url(arch_bm, arch, build_result)
                            if check_url_exists(url):
                                files.add((file_name, url))
                            else:
                                print("NOT FOUND:", k, file_name, url)
                    if files:
                        version_files[k] = files

                if fetch_tags:
                    run(["git", "fetch", "--tags"])

                def normalize_tag(tag):
                    if tag_to_version_re:
                        return tag_to_version_re.match(tag)[1]
                    return tag

                def get_tags():
                    if tag:
                        return [tag]
                    return run(["git", "tag", "-l"], text=True).splitlines(False)

                def get_file(args):
                    rpm_assets_dir, rpm_version, file_name, url = args
                    with s.get(url, stream=True) as r:
                        r.raise_for_status()
                        print(f"downloading file {rpm_version}/{file_name}")
                        rpm_asset = f"{rpm_assets_dir}/{file_name}"
                        with open(f"{rpm_asset}", "wb") as tmp_file:
                            for data in r.iter_content(chunk_size=1024 * 1024):
                                tmp_file.write(data)

                        return f"{rpm_asset}#{file_name}"

                def get_files(rpm_version):
                    rpm_assets_dir = f"{tmp_dir}/{rpm_version}"
                    makedirs(rpm_assets_dir, exist_ok=True)
                    return tpe.map(get_file, ((rpm_assets_dir, rpm_version, file_name, url)
                                              for file_name, url in version_files[rpm_version]))

                for current_tag in get_tags():
                    print()
                    print(f"processing tag {current_tag}")
                    rpm_version = normalize_tag(current_tag)
                    print(f"tag {current_tag} maps to rpm version {rpm_version}")

                    if not rpm_version in version_files:
                        print(f"no asset files found for tag {current_tag} rpm version {rpm_version}")
                        if tag:
                            sys.exit(100)
                        continue

                    try:
                        run(["gh", "release", "view", "--json", "tagName", current_tag], stderr=STDOUT, text=True)
                        release_found = True
                    except CalledProcessError as e:
                        if e.output and e.output.strip() != "release not found":
                            raise e
                        release_found = False

                    if not release_found:
                        print(f"creating release for tag {current_tag}")
                        exe(["gh", "release", "create", current_tag, "--verify-tag", "--generate-notes"], text=True)

                    if not release_found or clobber_assets:
                        upload_files = list(get_files(rpm_version))
                        print(f"uploading files into release {current_tag}: {', '.join(map(basename, upload_files))}")
                        exe(["gh", "release", "upload", current_tag] + upload_files + ["--clobber"], text=True)
                    else:
                        print(f"Release {current_tag} already exists and no '--clobber' is specified")


if __name__ == "__main__":
    main()
