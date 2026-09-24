# Installation telemetry prototype — distribution blocked

This directory does **not** contain a released Windows agent installer.

The former `bootstrapper/dist/trmm_web_installer.pyz` is a Python demo archive,
not a standalone Windows executable. Do not upload it to a download website,
rename it to `.exe`, or use a fixture payload as a production release.

Security corrections removed automatic consent, fixture installation, and
unconditional success reporting. The Python protocol harness now refuses to
download/install anything. The package boundary refuses to modify or remove an
existing installation without a verified package/ownership contract.

The FastAPI installation dashboard and telemetry store can display records, but
invitation creation and redemption return `RELEASE_NOT_READY`. Existing demo
attempt credentials are invalidated during database migration.

Outstanding work is listed in `diagnostics/INSTALLATION_CORRECTION_STATUS.md`.
The release checks intentionally remain failing until real artifacts exist.

`build_bootstrapper.py` no longer produces misleading zipapp output. Its PE
validator is an initial format/size check, not a signature verifier or proof that
an executable works on clean Windows.
