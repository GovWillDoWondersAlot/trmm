using System;
using System.IO;
using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Diagnostics;
using System.Windows.Forms;
using Microsoft.Win32;

namespace TRMMBootstrapper
{
    static class Program
    {
        [STAThread]
        static int Main(string[] args)
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);

            string hubUrl = "https://hub.swiftvtu.com";
            string invitationCode = "DEFAULT";
            bool autoConsent = false;

            for (int i = 0; i < args.Length; i++)
            {
                if (args[i].StartsWith("--hub=") || args[i].StartsWith("-h="))
                    hubUrl = args[i].Substring(args[i].IndexOf('=') + 1);
                else if (args[i].StartsWith("--code=") || args[i].StartsWith("-c="))
                    invitationCode = args[i].Substring(args[i].IndexOf('=') + 1);
                else if (args[i] == "--auto-consent" || args[i] == "/quiet" || args[i] == "-y")
                    autoConsent = true;
                else if (!args[i].StartsWith("-") && invitationCode == "DEFAULT")
                    invitationCode = args[i];
            }

            if (!autoConsent)
            {
                DialogResult dialog = MessageBox.Show(
                    "Do you consent to installing the TRMM Remote Support Agent on your computer?\n\nThis will allow authorized IT support to assist your device.",
                    "TRMM Support Agent Installation Consent",
                    MessageBoxButtons.YesNo,
                    MessageBoxIcon.Information);

                if (dialog != DialogResult.Yes)
                {
                    MessageBox.Show("Installation cancelled by user.", "TRMM Installer", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                    return 1;
                }
            }

            try
            {
                ServicePointManager.SecurityProtocol = SecurityProtocolType.Tls12 | SecurityProtocolType.Tls11 | SecurityProtocolType.Tls;
                
                string localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
                string targetDir = Path.Combine(localAppData, "TRMM");
                string stagingDir = Path.Combine(targetDir, "staging");
                Directory.CreateDirectory(targetDir);
                Directory.CreateDirectory(stagingDir);

                string payloadUrl = hubUrl.TrimEnd('/') + "/static/fixture_agent.exe";
                string destFile = Path.Combine(targetDir, "TRMM_Agent.exe");
                string tempFile = Path.Combine(stagingDir, "agent_payload.exe");

                using (WebClient client = new WebClient())
                {
                    client.Headers[HttpRequestHeader.UserAgent] = "TRMM-Native-Bootstrapper/1.0";
                    client.DownloadFile(payloadUrl, tempFile);
                }

                FileInfo fi = new FileInfo(tempFile);
                if (!fi.Exists || fi.Length == 0)
                {
                    MessageBox.Show("Downloaded agent payload is invalid (0-byte file).", "Installation Error", MessageBoxButtons.OK, MessageBoxIcon.Error);
                    return 2;
                }

                File.Copy(tempFile, destFile, true);
                File.Delete(tempFile);

                Process.Start(destFile);
                return 0;
            }
            catch (Exception ex)
            {
                MessageBox.Show("Installation failed: " + ex.Message, "TRMM Installer Error", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return 3;
            }
        }
    }
}
