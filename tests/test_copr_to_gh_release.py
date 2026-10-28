import importlib.util
import json
import sys
import unittest
from os.path import abspath, basename, dirname, join
from subprocess import CalledProcessError
from unittest import mock

SCRIPT = join(dirname(dirname(abspath(__file__))), "copr-to-gh-release.py")

COPR_API = "https://copr.fedorainfracloud.org/api_3"
REPO_URL = "https://download.copr.fedorainfracloud.org/results/karellen/karellen-sysbox"
PULP_URL = "https://packages.redhat.com/api/pulp-content/public-copr/karellen/karellen-sysbox"
TAG = "karellen-sysbox-0.7.1.8-1"


def load_script():
    spec = importlib.util.spec_from_file_location("copr_to_gh_release", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, url, status_code, body=b""):
        self.url = url
        self.status_code = status_code
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code} for {self.url}")

    def json(self):
        return json.loads(self.body)

    def iter_content(self, chunk_size=1):
        yield self.body


class FakeCopr:
    """Serves COPR API and download URLs; `redirects` maps legacy result URLs to their Pulp location."""

    def __init__(self, routes, redirects, unpublished):
        self.routes = routes
        self.redirects = redirects
        self.unpublished = unpublished

    def mount(self, prefix, adapter):
        pass

    def _resolve(self, url, allow_redirects):
        if url in self.redirects:
            if not allow_redirects:
                return FakeResponse(url, 301)
            url = self.redirects[url]
        if self.unpublished.get(url):
            self.unpublished[url] -= 1
            return FakeResponse(url, 404)
        if url not in self.routes:
            return FakeResponse(url, 404)
        body = self.routes[url]
        if isinstance(body, list):
            # Successive responses; the last one keeps being served
            body = body.pop(0) if len(body) > 1 else body[0]
        return FakeResponse(url, 200, body)

    def head(self, url, allow_redirects=False):
        return self._resolve(url, allow_redirects)

    def get(self, url, params=None, stream=False, allow_redirects=True):
        if params:
            url += "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        return self._resolve(url, allow_redirects)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def build(build_id, version, chroots, state="succeeded"):
    return {
        "id": build_id,
        "state": state,
        "ended_on": build_id,
        "chroots": list(chroots),
        "repo_url": REPO_URL,
        "source_package": {
            "name": "karellen-sysbox",
            "version": version,
            "url": f"{REPO_URL}/srpm-builds/{build_id:08d}/karellen-sysbox-{version}.src.rpm",
        },
    }


def rpm_result(version, arch):
    ver, rel = version.split("-")
    return {"name": "karellen-sysbox", "epoch": None, "version": ver, "release": rel, "arch": arch}


BUILD_LIST_URL = f"{COPR_API}/build/list?ownername=karellen&projectname=karellen-sysbox"
PROJECT_URL = f"{COPR_API}/project?ownername=karellen&projectname=karellen-sysbox"
NEW_BUILD_ID = 11181173


class CoprSite:
    """Routes of a COPR project holding the tagged 0.7.1.8-1 build, an older 0.7.1.5-1 build, and any added builds.

    A build's state is "failed" if any of its chroots failed, as in COPR. The project's enabled chroots default to
    every chroot of every build.
    """

    def __init__(self, chroot_states, pulp=True, project_chroots=None):
        self.routes = {}
        self.redirects = {}
        # URL -> number of requests that still see it as not yet published
        self.unpublished = {}
        self.pulp = pulp
        self.builds = []
        self.project_chroots = project_chroots
        self.add_build(11000001, "0.7.1.5-1", {"fedora-44-x86_64": "succeeded", "fedora-44-aarch64": "succeeded"})
        self.add_build(NEW_BUILD_ID, "0.7.1.8-1", chroot_states)

    def add_build(self, build_id, version, chroot_states):
        state = "failed" if "failed" in chroot_states.values() else "succeeded"
        b = build(build_id, version, list(chroot_states), state)
        self.builds.append(b)
        self.routes[b["source_package"]["url"]] = b"srpm"
        self.routes[f"{COPR_API}/build-chroot/list/?build_id={build_id}"] = json.dumps(
            {"items": [{"name": c, "state": s} for c, s in chroot_states.items()]}).encode()
        for chroot, chroot_state in chroot_states.items():
            if chroot_state == "succeeded":
                self.add_chroot_results(b, chroot)
        self.routes[BUILD_LIST_URL] = json.dumps({"items": self.builds}).encode()
        chroots = self.project_chroots
        if chroots is None:
            chroots = sorted({c for b in self.builds for c in b["chroots"]})
        self.routes[PROJECT_URL] = json.dumps({"chroot_repos": {c: f"{REPO_URL}/{c}/" for c in chroots}}).encode()
        return b

    def chroot_dir(self, b, chroot):
        return f"{REPO_URL}/{chroot}/{b['id']:08d}-karellen-sysbox"

    def add_chroot_results(self, b, chroot):
        version = b["source_package"]["version"]
        arch = chroot[chroot.rindex("-") + 1:]
        packages = [rpm_result(version, "src"), rpm_result(version, arch)]
        self.routes[f"{self.chroot_dir(b, chroot)}/results.json"] = json.dumps({"packages": packages}).encode()
        rpm = f"karellen-sysbox-{version}.{arch}.rpm"
        legacy = f"{self.chroot_dir(b, chroot)}/{rpm}"
        if self.pulp:
            pulp = f"{PULP_URL}/{chroot}/Packages/k/{rpm}"
            self.redirects[legacy] = pulp
            self.routes[pulp] = b"rpm"
        else:
            self.routes[legacy] = b"rpm"

    def remove(self, url):
        # A legacy URL keeps redirecting; it is the redirect target that stops being served
        del self.routes[self.redirects.get(url, url)]

    def new_build(self):
        return next(b for b in self.builds if b["id"] == NEW_BUILD_ID)

    def running_first(self, times):
        """The new build is reported as running by the first `times` build list requests."""
        finished = json.loads(self.routes[BUILD_LIST_URL])
        running = json.loads(self.routes[BUILD_LIST_URL])
        next(b for b in running["items"] if b["id"] == NEW_BUILD_ID).update(state="running", ended_on=None)
        self.routes[BUILD_LIST_URL] = [json.dumps(running).encode()] * times + [json.dumps(finished).encode()]


class CoprToGhReleaseTest(unittest.TestCase):
    def run_script(self, site, release_exists=False, wait_build=True):
        module = load_script()
        commands = []

        def run(cmd, **kwargs):
            commands.append(cmd)
            if cmd[:3] == ["gh", "release", "view"]:
                if release_exists:
                    return "{}"
                raise CalledProcessError(1, cmd, output="release not found")
            return ""

        def exe(cmd, **kwargs):
            commands.append(cmd)
            return 0

        argv = ["copr-to-gh-release.py",
                "--copr-owner-name=karellen",
                "--copr-project-name=karellen-sysbox",
                "--copr-package-name=karellen-sysbox",
                r"--tag-to-version-re=^karellen-sysbox-(\d.+)$",
                f"--tag={TAG}",
                "--clobber-assets"] + (["--wait-build"] if wait_build else [])
        exit_code = 0
        self.sleeps = []

        def sleep(seconds):
            self.sleeps.append(seconds)
            if len(self.sleeps) > 50:
                raise AssertionError("script keeps sleeping")
        with (mock.patch.object(module.requests, "Session",
                                lambda: FakeCopr(site.routes, site.redirects, site.unpublished)),
              mock.patch.object(module, "run", run),
              mock.patch.object(module, "exe", exe),
              mock.patch.object(module, "sleep", sleep),
              mock.patch.object(sys, "argv", argv)):
            try:
                module.main()
            except SystemExit as e:
                exit_code = e.code
        return exit_code, commands

    @staticmethod
    def uploaded(commands):
        for cmd in commands:
            if cmd[:3] == ["gh", "release", "upload"]:
                return sorted(asset.split("#")[1] for asset in cmd[4:] if "#" in asset)
        return None

    @staticmethod
    def created(commands):
        return any(cmd[:3] == ["gh", "release", "create"] for cmd in commands)

    def test_binary_rpms_redirected_to_pulp_are_uploaded(self):
        site = CoprSite({"fedora-44-x86_64": "succeeded", "fedora-44-aarch64": "succeeded"})

        exit_code, commands = self.run_script(site)

        self.assertIn(exit_code, (0, None))
        self.assertTrue(self.created(commands))
        self.assertEqual(self.uploaded(commands), [
            "fedora-44-karellen-sysbox-0.7.1.8-1.aarch64.rpm",
            "fedora-44-karellen-sysbox-0.7.1.8-1.x86_64.rpm",
            "karellen-sysbox-0.7.1.8-1.src.rpm",
        ])

    def test_binary_rpms_served_directly_are_uploaded(self):
        site = CoprSite({"fedora-44-x86_64": "succeeded", "fedora-44-aarch64": "succeeded"}, pulp=False)

        exit_code, commands = self.run_script(site)

        self.assertIn(exit_code, (0, None))
        self.assertEqual(self.uploaded(commands), [
            "fedora-44-karellen-sysbox-0.7.1.8-1.aarch64.rpm",
            "fedora-44-karellen-sysbox-0.7.1.8-1.x86_64.rpm",
            "karellen-sysbox-0.7.1.8-1.src.rpm",
        ])

    def test_unfetchable_rpm_of_succeeded_chroot_fails_without_releasing(self):
        site = CoprSite({"fedora-44-x86_64": "succeeded", "fedora-44-aarch64": "succeeded"})
        new = site.new_build()
        site.remove(f"{site.chroot_dir(new, 'fedora-44-x86_64')}/karellen-sysbox-0.7.1.8-1.x86_64.rpm")

        exit_code, commands = self.run_script(site)

        self.assertNotIn(exit_code, (0, None))
        self.assertEqual(len(self.sleeps), 10)
        self.assertFalse(self.created(commands))
        self.assertIsNone(self.uploaded(commands))

    def test_missing_results_of_succeeded_chroot_fails_without_releasing(self):
        site = CoprSite({"fedora-44-x86_64": "succeeded", "fedora-44-aarch64": "succeeded"})
        new = site.new_build()
        site.remove(f"{site.chroot_dir(new, 'fedora-44-aarch64')}/results.json")

        exit_code, commands = self.run_script(site)

        self.assertNotIn(exit_code, (0, None))
        self.assertFalse(self.created(commands))
        self.assertIsNone(self.uploaded(commands))

    def test_missing_assets_fail_clobbering_an_existing_release(self):
        site = CoprSite({"fedora-44-x86_64": "succeeded", "fedora-44-aarch64": "succeeded"})
        new = site.new_build()
        site.remove(f"{site.chroot_dir(new, 'fedora-44-x86_64')}/karellen-sysbox-0.7.1.8-1.x86_64.rpm")

        exit_code, commands = self.run_script(site, release_exists=True)

        self.assertNotIn(exit_code, (0, None))
        self.assertIsNone(self.uploaded(commands))

    def test_assets_published_late_are_awaited_and_uploaded(self):
        site = CoprSite({"fedora-44-x86_64": "succeeded", "fedora-44-aarch64": "succeeded"})
        new = site.new_build()
        site.unpublished[f"{PULP_URL}/fedora-44-x86_64/Packages/k/karellen-sysbox-0.7.1.8-1.x86_64.rpm"] = 3
        site.unpublished[f"{site.chroot_dir(new, 'fedora-44-aarch64')}/results.json"] = 2

        exit_code, commands = self.run_script(site)

        self.assertIn(exit_code, (0, None))
        self.assertEqual(len(self.sleeps), 3)
        self.assertEqual(self.uploaded(commands), [
            "fedora-44-karellen-sysbox-0.7.1.8-1.aarch64.rpm",
            "fedora-44-karellen-sysbox-0.7.1.8-1.x86_64.rpm",
            "karellen-sysbox-0.7.1.8-1.src.rpm",
        ])

    def test_running_build_is_awaited_with_wait_build(self):
        site = CoprSite({"fedora-44-x86_64": "succeeded", "fedora-44-aarch64": "succeeded"})
        site.running_first(2)

        exit_code, commands = self.run_script(site)

        self.assertIn(exit_code, (0, None))
        self.assertEqual(len(self.sleeps), 2)
        self.assertEqual(self.uploaded(commands), [
            "fedora-44-karellen-sysbox-0.7.1.8-1.aarch64.rpm",
            "fedora-44-karellen-sysbox-0.7.1.8-1.x86_64.rpm",
            "karellen-sysbox-0.7.1.8-1.src.rpm",
        ])

    def test_running_build_is_not_awaited_without_wait_build(self):
        site = CoprSite({"fedora-44-x86_64": "succeeded", "fedora-44-aarch64": "succeeded"})
        site.running_first(2)

        exit_code, commands = self.run_script(site, wait_build=False)

        self.assertEqual(self.sleeps, [])
        self.assertEqual(exit_code, 100)
        self.assertFalse(self.created(commands))

    def test_skipped_chroot_is_not_required(self):
        site = CoprSite({"fedora-44-x86_64": "succeeded", "fedora-44-aarch64": "skipped"})

        exit_code, commands = self.run_script(site)

        self.assertIn(exit_code, (0, None))
        self.assertEqual(self.uploaded(commands), [
            "fedora-44-karellen-sysbox-0.7.1.8-1.x86_64.rpm",
            "karellen-sysbox-0.7.1.8-1.src.rpm",
        ])

    def test_partially_failed_build_is_aggregated_with_resubmission_of_same_version(self):
        site = CoprSite({"fedora-44-x86_64": "succeeded", "fedora-44-aarch64": "failed",
                         "epel-9-x86_64": "succeeded", "epel-9-aarch64": "failed"})
        site.add_build(NEW_BUILD_ID + 1, "0.7.1.8-1", {"fedora-44-aarch64": "succeeded", "epel-9-aarch64": "failed"})
        site.add_build(NEW_BUILD_ID + 2, "0.7.1.8-1", {"epel-9-aarch64": "succeeded"})

        exit_code, commands = self.run_script(site)

        self.assertIn(exit_code, (0, None))
        self.assertEqual(self.uploaded(commands), [
            "epel-9-karellen-sysbox-0.7.1.8-1.aarch64.rpm",
            "epel-9-karellen-sysbox-0.7.1.8-1.x86_64.rpm",
            "fedora-44-karellen-sysbox-0.7.1.8-1.aarch64.rpm",
            "fedora-44-karellen-sysbox-0.7.1.8-1.x86_64.rpm",
            "karellen-sysbox-0.7.1.8-1.src.rpm",
        ])

    def test_failed_chroot_without_successful_resubmission_fails_without_releasing(self):
        site = CoprSite({"fedora-44-x86_64": "succeeded", "fedora-44-aarch64": "failed"})
        site.add_build(NEW_BUILD_ID + 1, "0.7.1.8-1", {"fedora-44-aarch64": "failed", "epel-9-x86_64": "succeeded"})

        exit_code, commands = self.run_script(site)

        self.assertNotIn(exit_code, (0, None))
        self.assertEqual(self.sleeps, [])
        self.assertFalse(self.created(commands))
        self.assertIsNone(self.uploaded(commands))

    def test_failed_chroot_is_not_aggregated_from_another_version(self):
        # 0.7.1.5-1 succeeded in fedora-44-aarch64, which must not stand in for 0.7.1.8-1
        site = CoprSite({"fedora-44-x86_64": "succeeded", "fedora-44-aarch64": "failed"})

        exit_code, commands = self.run_script(site)

        self.assertNotIn(exit_code, (0, None))
        self.assertFalse(self.created(commands))
        self.assertIsNone(self.uploaded(commands))

    def test_failed_chroot_disabled_in_project_is_not_required(self):
        site = CoprSite({"fedora-44-x86_64": "succeeded", "fedora-44-aarch64": "failed",
                         "epel-9-x86_64": "succeeded"},
                        project_chroots=["fedora-44-x86_64", "epel-9-x86_64", "epel-9-aarch64"])

        exit_code, commands = self.run_script(site)

        self.assertIn(exit_code, (0, None))
        self.assertEqual(self.uploaded(commands), [
            "epel-9-karellen-sysbox-0.7.1.8-1.x86_64.rpm",
            "fedora-44-karellen-sysbox-0.7.1.8-1.x86_64.rpm",
            "karellen-sysbox-0.7.1.8-1.src.rpm",
        ])

    def test_version_failed_in_every_chroot_fails_without_releasing(self):
        site = CoprSite({"fedora-44-x86_64": "failed", "fedora-44-aarch64": "failed"})

        exit_code, commands = self.run_script(site)

        self.assertNotIn(exit_code, (0, None))
        self.assertFalse(self.created(commands))
        self.assertIsNone(self.uploaded(commands))


if __name__ == "__main__":
    unittest.main()
