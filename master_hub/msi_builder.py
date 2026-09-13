"""
Native Windows Installer (.msi) Database Builder.
Generates genuine Microsoft .msi database packages using the Windows Installer Automation COM interface.
"""

import os
import gc
import uuid
import logging
import win32com.client
from typing import Dict, Any, List

logger = logging.getLogger("master_hub.msi_builder")

msiOpenDatabaseModeCreate = 3


class MsiBuilder:
    """Builds native .msi installer database packages."""

    @staticmethod
    def create_msi(
        output_msi_path: str,
        agent_id: str,
        endpoint_tag: str,
        payload_dir: str,
        arch: str = "x64",
    ) -> bool:
        db = None
        wi = None
        try:
            if os.path.exists(output_msi_path):
                try:
                    os.remove(output_msi_path)
                except Exception:
                    pass

            wi = win32com.client.Dispatch("WindowsInstaller.Installer")
            db = wi.OpenDatabase(output_msi_path, msiOpenDatabaseModeCreate)

            product_code = "{" + str(uuid.uuid4()).upper() + "}"
            upgrade_code = "{" + str(uuid.uuid4()).upper() + "}"

            # 1. Create Schema Tables
            tables = [
                "CREATE TABLE `Property` (`Property` CHAR(72) NOT NULL, `Value` CHAR(255) NOT NULL PRIMARY KEY `Property`)",
                "CREATE TABLE `Directory` (`Directory` CHAR(72) NOT NULL, `Directory_Parent` CHAR(72), `DefaultDir` CHAR(255) NOT NULL PRIMARY KEY `Directory`)",
                "CREATE TABLE `Feature` (`Feature` CHAR(38) NOT NULL, `Feature_Parent` CHAR(38), `Title` CHAR(64), `Description` CHAR(255), `Display` SHORT NOT NULL, `Level` SHORT NOT NULL, `Directory_` CHAR(72), `Attributes` SHORT NOT NULL PRIMARY KEY `Feature`)",
                "CREATE TABLE `Component` (`Component` CHAR(72) NOT NULL, `ComponentId` CHAR(38), `Directory_` CHAR(72) NOT NULL, `Attributes` SHORT NOT NULL, `Condition` CHAR(255), `KeyPath` CHAR(72) PRIMARY KEY `Component`)",
                "CREATE TABLE `File` (`File` CHAR(72) NOT NULL, `Component_` CHAR(72) NOT NULL, `FileName` CHAR(255) NOT NULL, `FileSize` LONG NOT NULL, `Version` CHAR(72), `Language` CHAR(20), `Attributes` SHORT, `Sequence` SHORT NOT NULL PRIMARY KEY `File`)",
                "CREATE TABLE `Media` (`DiskId` SHORT NOT NULL, `LastSequence` SHORT NOT NULL, `DiskPrompt` CHAR(64), `Cabinet` CHAR(255), `VolumeLabel` CHAR(32), `Source` CHAR(72) PRIMARY KEY `DiskId`)",
                "CREATE TABLE `FeatureComponents` (`Feature_` CHAR(38) NOT NULL, `Component_` CHAR(72) NOT NULL PRIMARY KEY `Feature_`, `Component_`)",
                "CREATE TABLE `InstallExecuteSequence` (`Action` CHAR(72) NOT NULL, `Condition` CHAR(255), `Sequence` SHORT PRIMARY KEY `Action`)",
                "CREATE TABLE `AdminExecuteSequence` (`Action` CHAR(72) NOT NULL, `Condition` CHAR(255), `Sequence` SHORT PRIMARY KEY `Action`)",
                "CREATE TABLE `CustomAction` (`Action` CHAR(72) NOT NULL, `Type` SHORT NOT NULL, `Source` CHAR(72), `Target` CHAR(255) PRIMARY KEY `Action`)",
                "CREATE TABLE `Registry` (`Registry` CHAR(72) NOT NULL, `Root` SHORT NOT NULL, `Key` CHAR(255) NOT NULL, `Name` CHAR(255), `Value` CHAR(255), `Component_` CHAR(72) NOT NULL PRIMARY KEY `Registry`)",
            ]

            for sql in tables:
                v = db.OpenView(sql)
                v.Execute()
                v.Close()

            # 2. Properties
            props = [
                f"INSERT INTO `Property` (`Property`, `Value`) VALUES ('ProductCode', '{product_code}')",
                f"INSERT INTO `Property` (`Property`, `Value`) VALUES ('ProductName', 'Tactical RMM Agent ({endpoint_tag})')",
                f"INSERT INTO `Property` (`Property`, `Value`) VALUES ('ProductVersion', '1.0.0')",
                f"INSERT INTO `Property` (`Property`, `Value`) VALUES ('ProductLanguage', '1033')",
                f"INSERT INTO `Property` (`Property`, `Value`) VALUES ('Manufacturer', 'Tactical RMM')",
                f"INSERT INTO `Property` (`Property`, `Value`) VALUES ('UpgradeCode', '{upgrade_code}')",
                f"INSERT INTO `Property` (`Property`, `Value`) VALUES ('ALLUSERS', '1')",
                f"INSERT INTO `Property` (`Property`, `Value`) VALUES ('INSTALLLEVEL', '1')",
            ]
            MsiBuilder._exec_queries(db, props)

            # 3. Directory
            prog_dir = "ProgramFiles64Folder" if arch == "x64" else "ProgramFilesFolder"
            dirs = [
                "INSERT INTO `Directory` (`Directory`, `Directory_Parent`, `DefaultDir`) VALUES ('TARGETDIR', NULL, 'SourceDir')",
                f"INSERT INTO `Directory` (`Directory`, `Directory_Parent`, `DefaultDir`) VALUES ('{prog_dir}', 'TARGETDIR', '.')",
                f"INSERT INTO `Directory` (`Directory`, `Directory_Parent`, `DefaultDir`) VALUES ('INSTALLDIR', '{prog_dir}', 'TRMM_Agent_{agent_id}')",
            ]
            MsiBuilder._exec_queries(db, dirs)

            # 4. Feature & Media
            feats = [
                "INSERT INTO `Feature` (`Feature`, `Feature_Parent`, `Title`, `Description`, `Display`, `Level`, `Directory_`, `Attributes`) VALUES ('Complete', NULL, 'TRMM Agent', 'Tactical RMM Agent Service', 1, 1, 'INSTALLDIR', 0)",
            ]
            media = [
                "INSERT INTO `Media` (`DiskId`, `LastSequence`, `DiskPrompt`, `Cabinet`, `VolumeLabel`, `Source`) VALUES (1, 9999, NULL, NULL, NULL, NULL)",
            ]
            MsiBuilder._exec_queries(db, feats)
            MsiBuilder._exec_queries(db, media)

            # 5. Component & Files
            comp_guid = "{" + str(uuid.uuid4()).upper() + "}"
            comp_sql = [
                f"INSERT INTO `Component` (`Component`, `ComponentId`, `Directory_`, `Attributes`, `Condition`, `KeyPath`) VALUES ('CompMain', '{comp_guid}', 'INSTALLDIR', 0, NULL, 'File_TRMM_Agent_exe_1')",
                "INSERT INTO `FeatureComponents` (`Feature_`, `Component_`) VALUES ('Complete', 'CompMain')",
            ]
            MsiBuilder._exec_queries(db, comp_sql)

            # Add files
            file_seq = 1
            file_sqls = []
            for root, _, filenames in os.walk(payload_dir):
                for fname in filenames:
                    fpath = os.path.join(root, fname)
                    fsize = os.path.getsize(fpath)
                    clean_id = f"File_{fname.replace('.', '_').replace('-', '_')}_{file_seq}"
                    file_sqls.append(
                        f"INSERT INTO `File` (`File`, `Component_`, `FileName`, `FileSize`, `Version`, `Language`, `Attributes`, `Sequence`) VALUES ('{clean_id}', 'CompMain', '{fname}', {fsize}, NULL, NULL, 512, {file_seq})"
                    )
                    file_seq += 1
            MsiBuilder._exec_queries(db, file_sqls)

            # 6. Custom Actions & Registry
            reg_sql = [
                f"INSERT INTO `Registry` (`Registry`, `Root`, `Key`, `Name`, `Value`, `Component_`) VALUES ('RegStartup', 2, 'Software\\Microsoft\\Windows\\CurrentVersion\\Run', 'TRMM_Agent_{agent_id}', '\"[INSTALLDIR]TRMM_Agent.exe\"', 'CompMain')",
            ]
            ca_sql = [
                f"INSERT INTO `CustomAction` (`Action`, `Type`, `Source`, `Target`) VALUES ('LaunchAgent', 3106, NULL, 'cmd.exe /c start \"\" \"[INSTALLDIR]TRMM_Agent.exe\"')",
            ]
            MsiBuilder._exec_queries(db, reg_sql)
            MsiBuilder._exec_queries(db, ca_sql)

            # 7. Sequence
            seq_sql = [
                "INSERT INTO `InstallExecuteSequence` (`Action`, `Condition`, `Sequence`) VALUES ('CostInitialize', NULL, 800)",
                "INSERT INTO `InstallExecuteSequence` (`Action`, `Condition`, `Sequence`) VALUES ('FileCost', NULL, 900)",
                "INSERT INTO `InstallExecuteSequence` (`Action`, `Condition`, `Sequence`) VALUES ('CostFinalize', NULL, 1000)",
                "INSERT INTO `InstallExecuteSequence` (`Action`, `Condition`, `Sequence`) VALUES ('InstallValidate', NULL, 1400)",
                "INSERT INTO `InstallExecuteSequence` (`Action`, `Condition`, `Sequence`) VALUES ('InstallInitialize', NULL, 1500)",
                "INSERT INTO `InstallExecuteSequence` (`Action`, `Condition`, `Sequence`) VALUES ('ProcessComponents', NULL, 1600)",
                "INSERT INTO `InstallExecuteSequence` (`Action`, `Condition`, `Sequence`) VALUES ('UnpublishFeatures', NULL, 1800)",
                "INSERT INTO `InstallExecuteSequence` (`Action`, `Condition`, `Sequence`) VALUES ('InstallFiles', NULL, 4000)",
                "INSERT INTO `InstallExecuteSequence` (`Action`, `Condition`, `Sequence`) VALUES ('WriteRegistryValues', NULL, 5000)",
                "INSERT INTO `InstallExecuteSequence` (`Action`, `Condition`, `Sequence`) VALUES ('LaunchAgent', 'NOT Installed', 6000)",
                "INSERT INTO `InstallExecuteSequence` (`Action`, `Condition`, `Sequence`) VALUES ('InstallFinalize', NULL, 6600)",
            ]
            MsiBuilder._exec_queries(db, seq_sql)

            # 8. Commit Database
            db.Commit()

            logger.info(f"Generated native MSI installer database: {output_msi_path}")
            return True

        except Exception as e:
            logger.error(f"Failed to generate MSI: {e}", exc_info=True)
            return False
        finally:
            if db:
                del db
            if wi:
                del wi
            gc.collect()

    @staticmethod
    def _exec_queries(db, queries: List[str]):
        for sql in queries:
            try:
                v = db.OpenView(sql)
                v.Execute()
                v.Close()
            except Exception as e:
                logger.debug(f"SQL execution error for '{sql}': {e}")
