Setting Up Instance On AWS SERVER

Step 1: Launch an EC2 Instance in AWS Console
Step 2: Log into your AWS Management Console
and navigate to EC2 > Instances > click Launch Instances.

Name: Enter TRMM-Master-Hub (or your choice).
Application and OS Images (Amazon Machine Image): Select Ubuntu.
Choose Ubuntu Server 24.04 LTS or Ubuntu Server 22.04 LTS (HVM), SSD Volume Type (64-bit x86).

Instance Type:
Recommended: t3.small (2 vCPU, 2 GiB RAM) or t3.medium (2 vCPU, 4 GiB RAM) for smooth fleet management.

Key Pair (login): Generate A new Key and Select the Key generated 

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

Do not Click on Lunch Instannce Yet  

DEPLOYMENT OF CODE FROM GIT TO UBUNTU SERVER

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
