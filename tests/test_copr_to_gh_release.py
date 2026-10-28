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


def build(build_id, version, chroots):
    return {
        "id": build_id,
        "state": "succeeded",
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


class CoprSite:
    """Builds the routes of a COPR project holding two builds: the tagged 0.7.1.8-1 and an older 0.7.1.5-1."""

    def __init__(self, chroot_states, pulp=True):
        self.routes = {}
        self.redirects = {}
        # URL -> number of requests that still see it as not yet published
        self.unpublished = {}
        self.pulp = pulp
        old = build(11000001, "0.7.1.5-1", ["fedora-44-x86_64", "fedora-44-aarch64"])
        new = build(11181173, "0.7.1.8-1", list(chroot_states))
        self.routes[f"{COPR_API}/build/list?ownername=karellen&projectname=karellen-sysbox"] = json.dumps(
            {"items": [old, new]}).encode()
        for b, states in ((old, {c: "succeeded" for c in old["chroots"]}), (new, chroot_states)):
            self.routes[b["source_package"]["url"]] = b"srpm"
            self.routes[f"{COPR_API}/build-chroot/list/?build_id={b['id']}"] = json.dumps(
                {"items": [{"name": c, "state": s} for c, s in states.items()]}).encode()
            for chroot, state in states.items():
                if state == "succeeded":
                    self.add_chroot_results(b, chroot)

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
        return json.loads(self.routes[f"{COPR_API}/build/list?ownername=karellen&projectname=karellen-sysbox"])[
            "items"][1]

    def running_first(self, times):
        """The new build is reported as running by the first `times` build list requests."""
        url = f"{COPR_API}/build/list?ownername=karellen&projectname=karellen-sysbox"
        finished = json.loads(self.routes[url])
        running = json.loads(self.routes[url])
        running["items"][1].update(state="running", ended_on=None)
        self.routes[url] = [json.dumps(running).encode()] * times + [json.dumps(finished).encode()]


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

    def test_chroot_that_did_not_succeed_is_not_required(self):
        site = CoprSite({"fedora-44-x86_64": "succeeded", "fedora-44-aarch64": "skipped",
                         "epel-9-aarch64": "failed"})

        exit_code, commands = self.run_script(site)

        self.assertIn(exit_code, (0, None))
        self.assertEqual(self.uploaded(commands), [
            "fedora-44-karellen-sysbox-0.7.1.8-1.x86_64.rpm",
            "karellen-sysbox-0.7.1.8-1.src.rpm",
        ])


if __name__ == "__main__":
    unittest.main()
