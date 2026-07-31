# Deploying the Facility Switch Gate on an Oracle Cloud (OCI) Always-Free VM

Goal: one always-on server with **one fixed public IP**. Everyone opens its
web page from any network; only this server talks to Uniware, so Uniware sees
a single IP that you whitelist once.

There are two phases:
- **Phase A — get it running** (you test it yourself, over http by IP).
- **Phase B — add HTTPS + share** (needs a subdomain; required before anyone
  else logs in, because passwords cross the network).

> ⚠️ Do NOT let teammates log in until Phase B (HTTPS) is done. Over plain
> http, passwords travel unencrypted.

---

## 0. What you'll end up with
```
Teammates' browsers ──HTTPS──► [ OCI VM: Caddy → gunicorn → app ] ──IPv4──► Uniware
   (any network)                 one fixed public IP (whitelisted)
```

---

## Phase A — Create the VM and run the app

### 1. Create an Oracle Cloud account
- Sign up at https://www.oracle.com/cloud/free/ (needs a card for identity
  verification; Always-Free resources are not charged).
- Pick a home region close to you.

### 2. Create an Always-Free VM instance
- Console → **Compute → Instances → Create instance**.
- **Image:** Canonical **Ubuntu 22.04**.
- **Shape:** an *Always Free eligible* shape — `VM.Standard.E2.1.Micro`
  (AMD) or `VM.Standard.A1.Flex` (ARM, 1 OCPU / 6 GB is plenty).
- **Networking:** let it create a VCN + public subnet; **Assign a public
  IPv4 address = Yes**.
- **SSH keys:** download / save the private key — you need it to log in.
- Create.

### 3. Make the public IP permanent (reserved)
- Instance → its VNIC → the public IP → **Edit → Reserved public IP** (or
  reserve one under Networking → IP Management and attach it).
- **Write this IP down — it's the one you'll whitelist in Uniware.**

### 4. Open ports 80 and 443 in OCI's firewall (Security List)
- Networking → your **VCN → Security Lists → Default Security List**.
- Add **Ingress Rules** (Source `0.0.0.0/0`, IP Protocol TCP):
  - Destination port **80**
  - Destination port **443**

### 5. SSH in and open the VM's *internal* firewall
Oracle's Ubuntu image ships with restrictive `iptables` — this is the #1
gotcha. Connect and open the ports:
```bash
ssh -i /path/to/your-key ubuntu@YOUR_PUBLIC_IP

sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

### 6. Install the essentials
```bash
sudo apt update
sudo apt install -y python3 python3-venv git
```

### 7. Get the code
```bash
cd ~
git clone https://github.com/eswarmediboina-jpg/Inventory-Allocation-Gate.git uniware_gate
cd uniware_gate
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 8. Configure secrets/env
```bash
cp deploy/gate.env.example gate.env
python3 -c "import secrets; print(secrets.token_hex(32))"   # copy the output
nano gate.env    # paste it as FLASK_SECRET_KEY; set paths; save
chmod 600 gate.env
```
- Put your BigQuery **service-account key** at `~/uniware_gate/sa.json`
  (upload via `scp`). Or skip BigQuery for now — logging just no-ops.

### 9. Quick test (Phase A, http by IP — you only)
```bash
set -a; source gate.env; set +a
.venv/bin/gunicorn --workers 2 --bind 0.0.0.0:80 app:app
```
Open `http://YOUR_PUBLIC_IP` in a browser. You should get the login page.
Log in (**only you**, since it's http) and confirm search works now that the
VM's IP is whitelisted. Then Ctrl-C to stop, and set up the service below.

---

## Phase B — Run it as a service + HTTPS, then share

### 10. Install the app as a background service
```bash
# Edit deploy/uniware-gate.service if your user/paths differ, then:
sudo cp deploy/uniware-gate.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now uniware-gate
systemctl status uniware-gate         # should be "active (running)"
```
(The service binds to `127.0.0.1:8000` — not public — because Caddy will
front it with HTTPS.)

### 11. Point your subdomain at the server
- In your DNS (for `zouk.co.in`), add an **A record**:
  - Name: `gate` (→ `gate.zouk.co.in`)
  - Value: **YOUR_PUBLIC_IP**
- Wait a few minutes for it to propagate (`ping gate.zouk.co.in` should show
  your IP).

### 12. Install Caddy for automatic HTTPS
```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy

# Put your subdomain in the Caddyfile, then install it:
nano deploy/Caddyfile          # change gate.zouk.co.in to your real subdomain
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
```
Caddy automatically fetches a TLS certificate. Visit
`https://gate.zouk.co.in` — you should get the login page over HTTPS. 🎉

### 13. Whitelist the IP in Uniware
Give your Uniware admin the **reserved public IP** from step 3 to whitelist
for API access. (Confirm the account also has REST API permission + facility
access to `zoukst` and `saleorderswitch`.)

### 14. Share with the team
Send them `https://gate.zouk.co.in`. Each person logs in with **their own**
Uniware credentials. Done.

---

## Updating the app later
```bash
cd ~/uniware_gate
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart uniware-gate
```

## Handy commands
```bash
journalctl -u uniware-gate -f     # app logs
sudo systemctl restart uniware-gate
sudo systemctl reload caddy       # after editing the Caddyfile
```

## Notes
- **IP stability:** an OCI *reserved* public IP does not change — good.
- **Free-tier reclamation:** Oracle may reclaim *idle* Always-Free VMs.
  A running service with traffic is fine; upgrading to Pay-As-You-Go (still
  within free limits) removes the risk entirely.
- **Keep it patched:** `sudo apt update && sudo apt upgrade` occasionally.
