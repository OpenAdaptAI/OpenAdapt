"""Portable security-call controls; no Windows API, installer or service runs.

The model implements only documented owner/DACL assignment and records calls.
It does not qualify Windows access checks, open handles or filesystem races.
"""

from __future__ import annotations

import importlib.util
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SOURCE = Path(__file__).parents[1] / "openadapt_flow/connector/windows_service.py"
SPEC = importlib.util.spec_from_file_location("_service_owner_control", SOURCE)
assert SPEC and SPEC.loader
ws = importlib.util.module_from_spec(SPEC)
# Keep this test portable even on Windows; every security/service call is fake.
with patch.object(sys, "platform", "portable-test"):
    SPEC.loader.exec_module(ws)

ADMIN = "S-1-5-32-544"
UNTRUSTED = "S-1-5-21-101-202-303-1001"


class SecurityCalls:
    OWNER_SECURITY_INFORMATION = 1
    DACL_SECURITY_INFORMATION = 4
    PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
    SE_FILE_OBJECT = 1
    ACL_REVISION = 2

    def __init__(self):
        self.owners = {}
        self.calls = []
        self.fail_set = False
        self.read_owners = {}
        self.reads = []

    @staticmethod
    def ACL():  # noqa: N802 - pywin32 spelling
        entries = []
        return SimpleNamespace(AddAccessAllowedAceEx=lambda *args: entries.append(args))

    @staticmethod
    def ConvertStringSidToSid(sid):  # noqa: N802
        return sid

    @staticmethod
    def ConvertSidToStringSid(sid):  # noqa: N802
        if sid is None:
            raise ValueError("missing owner")
        return sid

    @staticmethod
    def LookupAccountName(_system, _account):  # noqa: N802
        return ("service-sid", None, None)

    def SetNamedSecurityInfo(self, name, kind, flags, owner, group, dacl, sacl):  # noqa: N802
        self.calls.append((name, kind, flags, owner, group, dacl, sacl))
        if self.fail_set:
            raise OSError("synthetic owner assignment failure")
        # DACL replacement does not replace the owner. This is the original bug.
        if flags & self.OWNER_SECURITY_INFORMATION and owner is not None:
            self.owners[name] = owner

    def GetNamedSecurityInfo(self, name, kind, flags):  # noqa: N802
        assert kind == self.SE_FILE_OBJECT
        assert flags == self.OWNER_SECURITY_INFORMATION
        self.reads.append(name)
        owner = self.read_owners.get(name, self.owners[name])
        if isinstance(owner, Exception):
            raise owner
        return SimpleNamespace(GetSecurityDescriptorOwner=lambda: owner)

    def can_change_dacl_as_owner(self, name, principal):
        return self.owners[name] == principal


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oa-owner-control-")
        self.addCleanup(self.temporary.cleanup)
        self.config = Path(self.temporary.name) / "config"
        (self.config / "logs").mkdir(parents=True)
        (self.config / "nested").mkdir()
        for name in ("connector.toml", "nested/settings.txt", "logs/connector.log"):
            (self.config / name).write_text("own synthetic fixture\n", encoding="utf-8")
        self.paths = [self.config, *self.config.rglob("*")]
        self.security = SecurityCalls()
        self.security.owners = {str(path): UNTRUSTED for path in self.paths}
        modules = {
            "win32security": self.security,
            "win32con": SimpleNamespace(OBJECT_INHERIT_ACE=1, CONTAINER_INHERIT_ACE=2),
            "ntsecuritycon": SimpleNamespace(
                FILE_ALL_ACCESS=0x1F01FF,
                FILE_GENERIC_READ=0x120089,
                FILE_GENERIC_EXECUTE=0x1200A0,
            ),
        }
        self.addCleanup(patch.stopall)
        patch.dict(sys.modules, modules).start()

    def test_directory_owner_cannot_restore_read_after_dacl_replacement(self):
        ws.protect_config_directory(self.config)
        call = self.security.calls[-1]
        self.assertTrue(call[2] & self.security.OWNER_SECURITY_INFORMATION)
        self.assertEqual(call[3], ADMIN)
        self.assertFalse(
            self.security.can_change_dacl_as_owner(str(self.config), UNTRUSTED)
        )
        self.assertTrue(call[2] & self.security.DACL_SECURITY_INFORMATION)
        self.assertTrue(call[2] & self.security.PROTECTED_DACL_SECURITY_INFORMATION)

    def test_original_dacl_only_call_preserves_untrusted_owner_authority(self):
        self.security.SetNamedSecurityInfo(
            str(self.config),
            self.security.SE_FILE_OBJECT,
            self.security.DACL_SECURITY_INFORMATION
            | self.security.PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            self.security.ACL(),
            None,
        )
        self.assertTrue(
            self.security.can_change_dacl_as_owner(str(self.config), UNTRUSTED)
        )

    def install(self, failure=None, skip_owner=None):
        calls = []
        state = {"start_type": None}

        def create(*args):
            state["start_type"] = args[5]
            calls.append(("create", args[5]))
            return "owned-service"

        def change(_handle, _kind, start_type, *_args):
            if failure == "enable":
                raise OSError("synthetic enable failure")
            state["start_type"] = start_type
            calls.append(("start_type", start_type))

        def run(command):
            # All OS command execution is replaced by these recorded DATA effects.
            self.assertEqual(state["start_type"], 4)
            calls.append(tuple(command))
            stage = (
                "owner"
                if "/setowner" in command
                else "reset"
                if "/reset" in command
                else "logs"
                if "/grant" in command
                else "recovery"
            )
            if failure == stage:
                raise OSError("synthetic " + stage + " failure")
            if stage == "owner":
                self.assertEqual(
                    command,
                    [
                        "icacls",
                        str(self.config / "*"),
                        "/setowner",
                        "*" + ADMIN,
                        "/T",
                        "/Q",
                    ],
                )
                for path in self.paths[1:]:
                    if path != skip_owner:
                        self.security.owners[str(path)] = ADMIN
            if failure == "verify-enumeration" and stage == "logs":
                patch.object(
                    ws.os,
                    "scandir",
                    side_effect=PermissionError("synthetic enumeration failure"),
                ).start()

        def configure(_handle, kind, _value):
            calls.append(("config", kind))
            if failure == ("description" if kind == 1 else "delay"):
                raise OSError("synthetic service metadata failure")

        service = SimpleNamespace(
            SC_MANAGER_ALL_ACCESS=1,
            SERVICE_ALL_ACCESS=2,
            SERVICE_WIN32_OWN_PROCESS=16,
            SERVICE_DISABLED=4,
            SERVICE_AUTO_START=2,
            SERVICE_ERROR_NORMAL=1,
            SERVICE_NO_CHANGE=0xFFFFFFFF,
            SERVICE_CONFIG_DESCRIPTION=1,
            SERVICE_CONFIG_DELAYED_AUTO_START_INFO=3,
            OpenSCManager=lambda *_args: "owned-scm",
            CreateService=create,
            ChangeServiceConfig=change,
            ChangeServiceConfig2=configure,
            CloseServiceHandle=lambda handle: calls.append(("close", handle)),
        )
        patch.object(ws, "win32service", service, create=True).start()
        patch.object(ws, "_require_pywin32", return_value=None).start()
        patch.object(ws, "service_python", return_value="own-python").start()
        patch.object(ws, "_run", side_effect=run).start()
        return calls, state

    def test_install_establishes_every_owner_before_enabling_auto_start(self):
        calls, state = self.install()
        ws.install_service(self.config)
        self.assertEqual(calls[0], ("create", 4))
        self.assertEqual(state["start_type"], 2)
        self.assertEqual(
            calls[-4:],
            [
                ("config", 3),
                ("start_type", 2),
                ("close", "owned-service"),
                ("close", "owned-scm"),
            ],
        )
        self.assertEqual(set(self.security.reads), {str(path) for path in self.paths})
        for path in self.paths:
            self.assertEqual(self.security.owners[str(path)], ADMIN)
            self.assertFalse(
                self.security.can_change_dacl_as_owner(str(path), UNTRUSTED)
            )
        commands = [c for c in calls if c[0] == "icacls"]
        self.assertIn("/setowner", commands[0])
        self.assertIn("/reset", commands[1])
        self.assertEqual(commands[2][-1], ws.SERVICE_ACCOUNT + ":(OI)(CI)M")

    def test_each_descendant_owner_must_be_changed_even_if_acl_reset_succeeds(self):
        for path in self.paths[1:]:
            with self.subTest(path=path.relative_to(self.config)):
                self.security.owners = {str(p): UNTRUSTED for p in self.paths}
                calls, state = self.install(skip_owner=path)
                with self.assertRaisesRegex(
                    RuntimeError, "ownership was not established"
                ):
                    ws.install_service(self.config)
                self.assertEqual(state["start_type"], 4)
                self.assertNotIn(("start_type", 2), calls)
                self.assertTrue(
                    self.security.can_change_dacl_as_owner(str(path), UNTRUSTED)
                )

    def test_security_command_failures_leave_new_service_disabled(self):
        for stage in (
            "owner",
            "reset",
            "logs",
            "recovery",
            "verify-enumeration",
            "description",
            "delay",
            "enable",
        ):
            with self.subTest(stage=stage):
                calls, state = self.install(failure=stage)
                with self.assertRaises(OSError):
                    ws.install_service(self.config)
                self.assertEqual(state["start_type"], 4)
                self.assertNotIn(("start_type", 2), calls)
                self.assertEqual(
                    calls[-2:], [("close", "owned-service"), ("close", "owned-scm")]
                )
                patch.stopall()
                patch.dict(
                    sys.modules,
                    {
                        "win32security": self.security,
                        "win32con": SimpleNamespace(
                            OBJECT_INHERIT_ACE=1, CONTAINER_INHERIT_ACE=2
                        ),
                        "ntsecuritycon": SimpleNamespace(
                            FILE_ALL_ACCESS=0x1F01FF,
                            FILE_GENERIC_READ=0x120089,
                            FILE_GENERIC_EXECUTE=0x1200A0,
                        ),
                    },
                ).start()

    def test_failed_root_owner_assignment_never_enables_service(self):
        self.security.fail_set = True
        calls, state = self.install()
        with self.assertRaises(OSError):
            ws.install_service(self.config)
        self.assertEqual(state["start_type"], 4)
        self.assertNotIn(("start_type", 2), calls)

    def test_missing_mismatched_and_unreadable_owners_fail_closed(self):
        for path in self.paths:
            for owner in (None, UNTRUSTED, OSError("synthetic owner read failure")):
                with self.subTest(
                    path=path.relative_to(self.config), owner=type(owner).__name__
                ):
                    self.security.owners = {str(p): ADMIN for p in self.paths}
                    self.security.read_owners = {str(path): owner}
                    with self.assertRaises((OSError, RuntimeError)):
                        ws.verify_config_ownership(self.config)

    def test_owner_readback_failure_keeps_service_disabled(self):
        for path in (self.config, ws.config_file(self.config)):
            with self.subTest(path=path):
                self.security.read_owners = {str(path): UNTRUSTED}
                calls, state = self.install()
                with self.assertRaisesRegex(
                    RuntimeError, "ownership was not established"
                ):
                    ws.install_service(self.config)
                self.assertEqual(state["start_type"], 4)
                self.assertNotIn(("start_type", 2), calls)

    def test_tree_enumeration_errors_are_not_treated_as_empty_success(self):
        calls, state = self.install()
        with patch.object(
            ws.os,
            "scandir",
            side_effect=PermissionError("synthetic enumeration failure"),
        ):
            with self.assertRaises(PermissionError):
                ws.install_service(self.config)
        self.assertEqual(self.security.calls, [])
        self.assertEqual(state["start_type"], 4)
        self.assertNotIn(("start_type", 2), calls)

    def test_links_and_reparse_points_refuse_before_security_mutation(self):
        target = Path(self.temporary.name) / "outside.txt"
        target.write_text("own outside fixture", encoding="utf-8")
        (self.config / "alias").symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, "link or reparse point"):
            ws.protect_config_directory(self.config)
        self.assertEqual(self.security.calls, [])
        (self.config / "alias").unlink()
        info = self.config.lstat()
        with patch.object(
            ws.Path,
            "lstat",
            return_value=SimpleNamespace(
                st_mode=info.st_mode,
                st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "link or reparse point"):
                ws.protect_config_directory(self.config)
        self.assertEqual(self.security.calls, [])


if __name__ == "__main__":
    unittest.main()
