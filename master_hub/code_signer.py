"""
Tactical RMM — Enterprise Code Signing & Certificate Engine.
Digitally signs Windows executables (.exe), PowerShell installers (.ps1),
and Android APK packages (.apk) to establish verified publisher identity,
satisfy Microsoft Authenticode, and prevent Antivirus / SmartScreen / Play Protect flags.
"""

import os
import sys
import shutil
import logging
import datetime
import subprocess
from typing import Dict, Any, Optional

logger = logging.getLogger("master_hub.code_signer")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CERTS_DIR = os.path.join(ROOT_DIR, "master_hub", "certs")
PAYLOADS_DIR = os.path.join(ROOT_DIR, "master_hub", "android", "payloads")
os.makedirs(CERTS_DIR, exist_ok=True)


class CodeSigner:
    """Manages digital certificates, Authenticode signing, and Android release keystores."""

    DEFAULT_PFX_PASS = "trmm_signing_key"
    DEFAULT_KEYSTORE_PASS = "trmm_release_pass"
    DEFAULT_KEYSTORE_ALIAS = "trmm_release"

    @classmethod
    def get_or_create_certificate(cls) -> Dict[str, str]:
        """
        Retrieves or generates an Authenticode-compliant Code Signing Certificate.
        1. Checks for custom commercial certificate: master_hub/certs/code_signing.pfx
        2. Falls back to persistent enterprise self-signed certificate: master_hub/certs/trmm_codesign.pfx
        3. If neither exists, generates a 10-year RSA 2048/4096-bit Code Signing Certificate with EKU 1.3.6.1.5.5.7.3.3
        """
        os.makedirs(CERTS_DIR, exist_ok=True)

        # 1. Custom commercial PFX certificate
        custom_pfx = os.path.join(CERTS_DIR, "code_signing.pfx")
        if os.path.isfile(custom_pfx):
            pass_file = os.path.join(CERTS_DIR, "code_signing.pass")
            password = os.environ.get("TRMM_CODESIGN_PASS", "")
            if os.path.isfile(pass_file):
                try:
                    with open(pass_file, "r", encoding="utf-8") as f:
                        password = f.read().strip()
                except Exception:
                    pass
            cer_file = os.path.join(CERTS_DIR, "code_signing.cer")
            return {
                "pfx_path": custom_pfx,
                "password": password,
                "cer_path": cer_file if os.path.isfile(cer_file) else custom_pfx,
                "is_custom": True
            }

        # 2. Existing auto-generated enterprise PFX certificate
        auto_pfx = os.path.join(CERTS_DIR, "trmm_codesign.pfx")
        auto_cer = os.path.join(CERTS_DIR, "trmm_root_ca.cer")
        if os.path.isfile(auto_pfx) and os.path.isfile(auto_cer):
            return {
                "pfx_path": auto_pfx,
                "password": cls.DEFAULT_PFX_PASS,
                "cer_path": auto_cer,
                "is_custom": False
            }

        # 3. Generate new 10-year Code Signing Certificate using cryptography
        try:
            from cryptography import x509
            from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            from cryptography.hazmat.primitives.serialization import pkcs12

            logger.info("Generating new 10-year Enterprise Code Signing Certificate (RSA 2048-bit, SHA-256)...")
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

            subject = issuer = x509.Name([
                x509.NameAttribute(NameOID.COMMON_NAME, "Tactical RMM Software Publisher"),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Tactical RMM Inc."),
                x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "Endpoint Security"),
                x509.NameAttribute(NameOID.LOCALITY_NAME, "San Francisco"),
                x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "California"),
                x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
            ])

            now = datetime.datetime.now(datetime.timezone.utc)
            cert = (
                x509.CertificateBuilder()
                .subject_name(subject)
                .issuer_name(issuer)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=3650))
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .add_extension(
                    x509.KeyUsage(
                        digital_signature=True,
                        content_commitment=False,
                        key_encipherment=False,
                        data_encipherment=False,
                        key_agreement=False,
                        key_cert_sign=True,
                        crl_sign=True,
                        encipher_only=False,
                        decipher_only=False,
                    ),
                    critical=True,
                )
                .add_extension(
                    x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CODE_SIGNING]),
                    critical=False,
                )
                .sign(key, hashes.SHA256())
            )

            # Export PKCS#12 (.pfx)
            pfx_bytes = pkcs12.serialize_key_and_certificates(
                name=b"trmm_codesign",
                key=key,
                cert=cert,
                cas=None,
                encryption_algorithm=serialization.BestAvailableEncryption(cls.DEFAULT_PFX_PASS.encode("utf-8"))
            )
            with open(auto_pfx, "wb") as pf:
                pf.write(pfx_bytes)

            # Export X.509 DER (.cer) for Windows Certificate Store import
            der_bytes = cert.public_bytes(serialization.Encoding.DER)
            with open(auto_cer, "wb") as cf:
                cf.write(der_bytes)

            # Export PEM (.crt and .key)
            with open(os.path.join(CERTS_DIR, "trmm_codesign.crt"), "wb") as crtf:
                crtf.write(cert.public_bytes(serialization.Encoding.PEM))
            with open(os.path.join(CERTS_DIR, "trmm_codesign.key"), "wb") as kf:
                kf.write(key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.TraditionalOpenSSL,
                    encryption_algorithm=serialization.NoEncryption()
                ))

            logger.info(f"Enterprise Code Signing Certificate successfully generated at: {auto_pfx}")
            return {
                "pfx_path": auto_pfx,
                "password": cls.DEFAULT_PFX_PASS,
                "cer_path": auto_cer,
                "is_custom": False
            }
        except Exception as e:
            logger.error(f"Failed to generate code signing certificate: {e}", exc_info=True)
            return {}

    @classmethod
    def sign_windows_executable(cls, exe_path: str, app_title: str = "Tactical RMM Agent") -> bool:
        """
        Digitally signs a Windows PE binary (.exe or .dll) using Microsoft Authenticode.
        Supports:
        - osslsigncode (Standard on Linux & Ubuntu via 'sudo apt install -y osslsigncode')
        - signtool.exe (Windows SDK)
        - PowerShell Set-AuthenticodeSignature (Native on all Windows machines)
        """
        if not (exe_path and os.path.isfile(exe_path)):
            logger.warning(f"Executable not found for signing: {exe_path}")
            return False

        cert_info = cls.get_or_create_certificate()
        pfx_path = cert_info.get("pfx_path")
        password = cert_info.get("password", "")
        if not (pfx_path and os.path.isfile(pfx_path)):
            logger.warning("No Code Signing Certificate available to sign executable.")
            return False

        # 1. Try osslsigncode (Universal on Linux and cross-platform)
        osslsigncode_bin = shutil.which("osslsigncode")
        if osslsigncode_bin:
            try:
                signed_tmp = exe_path + ".signed.tmp"
                # Timestamp servers to try
                ts_servers = [
                    "http://timestamp.digicert.com",
                    "http://timestamp.sectigo.com",
                    "http://timestamp.comodoca.com"
                ]
                signed = False
                for ts in ts_servers:
                    cmd = [
                        osslsigncode_bin, "sign",
                        "-pkcs12", pfx_path,
                        "-pass", password or "",
                        "-n", app_title,
                        "-i", "https://trmm.local",
                        "-t", ts,
                        "-in", exe_path,
                        "-out", signed_tmp
                    ]
                    res = subprocess.run(cmd, capture_output=True, text=True)
                    if res.returncode == 0 and os.path.isfile(signed_tmp):
                        shutil.move(signed_tmp, exe_path)
                        signed = True
                        logger.info(f"Authenticode signed {exe_path} via osslsigncode (timestamp: {ts})")
                        break
                    else:
                        logger.debug(f"osslsigncode with {ts} failed ({res.stderr[:200]}), trying next...")

                if not signed:
                    # Try without timestamp if external internet is blocked
                    cmd_nots = [
                        osslsigncode_bin, "sign",
                        "-pkcs12", pfx_path,
                        "-pass", password or "",
                        "-n", app_title,
                        "-in", exe_path,
                        "-out", signed_tmp
                    ]
                    res_nots = subprocess.run(cmd_nots, capture_output=True, text=True)
                    if res_nots.returncode == 0 and os.path.isfile(signed_tmp):
                        shutil.move(signed_tmp, exe_path)
                        signed = True
                        logger.info(f"Authenticode signed {exe_path} via osslsigncode (no timestamp)")

                if signed:
                    return True
            except Exception as e:
                logger.warning(f"Error running osslsigncode: {e}")

        # 2. On Windows: Try signtool.exe
        signtool_bin = shutil.which("signtool") or shutil.which("signtool.exe")
        if signtool_bin:
            try:
                sign_cmd = [
                    signtool_bin, "sign",
                    "/f", pfx_path,
                    "/p", password or "",
                    "/fd", "SHA256",
                    "/tr", "http://timestamp.digicert.com",
                    "/td", "SHA256",
                    "/d", app_title,
                    exe_path
                ]
                res = subprocess.run(sign_cmd, capture_output=True, text=True)
                if res.returncode == 0:
                    logger.info(f"Authenticode signed {exe_path} via signtool.exe")
                    return True
                else:
                    logger.debug(f"signtool.exe returned {res.returncode}: {res.stderr}")
            except Exception as e:
                logger.debug(f"signtool failed: {e}")

        # 3. On Windows: Try native PowerShell Set-AuthenticodeSignature
        if sys.platform == "win32":
            import tempfile
            ps_file = None
            try:
                ps_script = f"""
$ErrorActionPreference = 'Stop'
$pfxPath = '{os.path.abspath(pfx_path).replace(chr(92), "/")}'
$targetPath = '{os.path.abspath(exe_path).replace(chr(92), "/")}'
$flags = [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::Exportable -bor [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::PersistKeySet
$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($pfxPath, '{password}', $flags)
try {{
    $sig = Set-AuthenticodeSignature -FilePath $targetPath -Certificate $cert -TimestampServer 'http://timestamp.digicert.com' -HashAlgorithm SHA256
}} catch {{
    $sig = Set-AuthenticodeSignature -FilePath $targetPath -Certificate $cert -HashAlgorithm SHA256
}}
if ($sig.Status -in @('Valid', 'UnknownError')) {{
    Write-Output "SIGNED_OK"
}} else {{
    Write-Output "STATUS:$($sig.Status):$($sig.StatusMessage)"
}}
"""
                with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as f:
                    f.write(ps_script)
                    ps_file = f.name

                res = subprocess.run(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps_file],
                    capture_output=True, text=True
                )
                if "SIGNED_OK" in res.stdout:
                    logger.info(f"Authenticode signed {exe_path} via PowerShell Set-AuthenticodeSignature")
                    return True
                else:
                    logger.debug(f"PowerShell signing output: {res.stdout.strip()} | {res.stderr.strip()}")
            except Exception as e:
                logger.debug(f"PowerShell signing failed: {e}")
            finally:
                if ps_file and os.path.exists(ps_file):
                    try:
                        os.remove(ps_file)
                    except Exception:
                        pass

        logger.info(f"Binary generated without Authenticode signature (install osslsigncode on Linux or signtool on Windows for automatic signing).")
        return False

    @classmethod
    def sign_powershell_script(cls, ps1_path: str) -> bool:
        """
        Digitally signs a PowerShell (.ps1) script using Set-AuthenticodeSignature.
        Adds verified digital signature block to script, establishing trusted publisher.
        """
        if not (ps1_path and os.path.isfile(ps1_path)):
            return False

        cert_info = cls.get_or_create_certificate()
        pfx_path = cert_info.get("pfx_path")
        password = cert_info.get("password", "")
        if not (pfx_path and os.path.isfile(pfx_path)):
            return False

        pwsh_bin = shutil.which("powershell") or shutil.which("pwsh")
        if not pwsh_bin:
            return False

        import tempfile
        runner_ps = None
        try:
            pfx_clean = os.path.abspath(pfx_path).replace(chr(92), "/")
            target_clean = os.path.abspath(ps1_path).replace(chr(92), "/")
            script = f"""
$ErrorActionPreference = 'Stop'
$pfx = '{pfx_clean}'
$tgt = '{target_clean}'
$flags = [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::Exportable -bor [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::PersistKeySet
$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($pfx, '{password}', $flags)
try {{
    $sig = Set-AuthenticodeSignature -FilePath $tgt -Certificate $cert -TimestampServer 'http://timestamp.digicert.com' -HashAlgorithm SHA256
}} catch {{
    $sig = Set-AuthenticodeSignature -FilePath $tgt -Certificate $cert -HashAlgorithm SHA256
}}
if ($sig.Status -in @('Valid', 'UnknownError')) {{
    Write-Output "SIGNED_OK"
}} else {{
    Write-Output "STATUS:$($sig.Status):$($sig.StatusMessage)"
}}
"""
            with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as f:
                f.write(script)
                runner_ps = f.name

            res = subprocess.run([pwsh_bin, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", runner_ps],
                                 capture_output=True, text=True)
            if "SIGNED_OK" in res.stdout:
                logger.info(f"Authenticode signed PowerShell script: {ps1_path}")
                return True
            else:
                logger.debug(f"PowerShell script signing status: {res.stdout.strip()}")
        except Exception as e:
            logger.debug(f"PowerShell signing error: {e}")
        finally:
            if runner_ps and os.path.exists(runner_ps):
                try:
                    os.remove(runner_ps)
                except Exception:
                    pass
        return False

    @classmethod
    def get_android_release_keystore(cls) -> Dict[str, str]:
        """
        Retrieves or generates a Production Release Keystore for Android APKs.
        Uses corporate organizational identity (CN=Tactical Endpoint Management)
        and release alias 'trmm_release' to prevent Google Play Protect debug warnings.
        """
        # 1. Check for custom release keystore
        custom_keystore = os.path.join(CERTS_DIR, "release.keystore")
        if not os.path.isfile(custom_keystore):
            custom_keystore = os.path.join(PAYLOADS_DIR, "release.keystore")

        if os.path.isfile(custom_keystore):
            return {
                "keystore": custom_keystore,
                "alias": cls.DEFAULT_KEYSTORE_ALIAS,
                "storepass": cls.DEFAULT_KEYSTORE_PASS,
                "keypass": cls.DEFAULT_KEYSTORE_PASS
            }

        # 2. Auto-generate production release keystore with keytool
        keytool = shutil.which("keytool")
        target_keystore = os.path.join(CERTS_DIR, "release.keystore")
        if keytool:
            try:
                os.makedirs(CERTS_DIR, exist_ok=True)
                dname = "CN=Tactical Endpoint Management, OU=Mobile Security, O=Tactical RMM Inc., L=San Francisco, ST=California, C=US"
                cmd = [
                    keytool, "-genkeypair", "-v",
                    "-keystore", target_keystore,
                    "-storepass", cls.DEFAULT_KEYSTORE_PASS,
                    "-alias", cls.DEFAULT_KEYSTORE_ALIAS,
                    "-keypass", cls.DEFAULT_KEYSTORE_PASS,
                    "-keyalg", "RSA",
                    "-keysize", "2048",
                    "-validity", "10000",
                    "-dname", dname
                ]
                subprocess.run(cmd, capture_output=True, check=True)
                logger.info(f"Generated Production Release Keystore at {target_keystore} (Alias: {cls.DEFAULT_KEYSTORE_ALIAS})")
                return {
                    "keystore": target_keystore,
                    "alias": cls.DEFAULT_KEYSTORE_ALIAS,
                    "storepass": cls.DEFAULT_KEYSTORE_PASS,
                    "keypass": cls.DEFAULT_KEYSTORE_PASS
                }
            except Exception as e:
                logger.warning(f"Failed to generate release.keystore via keytool: {e}")

        # Fallback to legacy path if keytool generated previously
        legacy_keystore = os.path.join(PAYLOADS_DIR, "debug.keystore")
        return {
            "keystore": legacy_keystore,
            "alias": "androiddebugkey",
            "storepass": "android",
            "keypass": "android"
        }
