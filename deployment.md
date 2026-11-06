Here is the step-by-step guide to launch and deploy your project on an AWS EC2 instance.

You already have an SSH key pair (trmm-key.pem) in your project folder, and I have created an automated deployment script (deployment/setup_aws_ubuntu.sh) pushed to your repository that handles Python, virtualenv, dependencies, Nginx WebSocket reverse proxy, and the systemd background service.

Step 1: Launch an EC2 Instance in AWS Console
Log into your AWS Management Console
and navigate to EC2 > Instances > click Launch Instances.
Name: Enter TRMM-Master-Hub (or your choice).
Application and OS Images (Amazon Machine Image):
Select Ubuntu.
Choose Ubuntu Server 24.04 LTS or Ubuntu Server 22.04 LTS (HVM), SSD Volume Type (64-bit x86).
Instance Type:
Recommended: t3.small (2 vCPU, 2 GiB RAM) or t3.medium (2 vCPU, 4 GiB RAM) for smooth fleet management.
Key Pair (login):
Select trmm-key (this matches the trmm-key.pem file on your machine).
Network Settings (Security Group):
Select Create security group and check:
☑ Allow SSH traffic from Anywhere (0.0.0.0/0 or your My IP) — Port 22
☑ Allow HTTP traffic from the internet — Port 80
☑ Allow HTTPS traffic from the internet — Port 443
Click Add security group rule (optional for direct testing):
Type: Custom TCP, Port: 8000, Source: 0.0.0.0/0.
Storage:
Set size to 20 GiB or 30 GiB (gp3).
Step 2: One-Click Automated Deployment

You can deploy the instance in either of two ways:

Option A: Zero-Touch via "User Data" (Recommended)

Before clicking Launch Instance, expand Advanced details at the bottom of the launch page, scroll to User data, and paste this:

bash
#!/bin/bash
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y git curl
curl -sSL https://raw.githubusercontent.com/Samace-TechLab/trmm/main/deployment/setup_aws_ubuntu.sh -o /tmp/setup.sh
chmod +x /tmp/setup.sh
sudo -u ubuntu /tmp/setup.sh

Click Launch Instance. The server will self-provision and start the TRMM hub in ~2 minutes without needing manual SSH commands!

Option B: Manual SSH Deployment

If your instance is already launched or you prefer running it interactively:

Copy your instance's Public IPv4 address from the AWS Console (e.g., 54.x.x.x).
Open PowerShell in your local project folder (C:\Users\Samace Tech Lab\Desktop\TRMM) and connect:
powershell
ssh -i "trmm-key.pem" ubuntu@YOUR_INSTANCE_PUBLIC_IP

(Note: On Windows, ensure permissions on the key are clean if prompted: icacls.exe trmm-key.pem /inheritance:r /grant:r "$($env:USERNAME):(R)")

Once logged into the Ubuntu server, run the setup script:
bash
curl -sSL https://raw.githubusercontent.com/Samace-TechLab/trmm/main/deployment/setup_aws_ubuntu.sh | bash
Step 3: Accessing Your Deployed Hub
Open your browser and go to:
http://YOUR_INSTANCE_PUBLIC_IP
Log into the console using the Master All-Access Key:
0235059609567482
Step 4: Optional Custom Domain & SSL (HTTPS)

Once you point your domain (e.g., rmm.yourdomain.com) to your EC2 Public IP address via an A record:

bash
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d rmm.yourdomain.com

Certbot will automatically install the Let's Encrypt certificate and upgrade Nginx to secure HTTPS with WebSockets enabled.
