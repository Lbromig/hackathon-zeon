# UFACTORY xArm 6: safe first connection

This starter verifies that a computer can reach an xArm controller and reads
basic status information. It does **not** enable motors, clear errors, change
settings, release brakes, or send movement commands.

## 1. Make the hardware safe

Before connecting:

1. Keep the emergency stop reachable.
2. Clear people, loose objects, and cables from the arm's full reach.
3. Do not enable the arm or release its brakes yet.
4. Find the controller IP printed on the side of the control box. It should
   look like `192.168.1.xxx`.

## 2. Configure a direct Ethernet connection on macOS

The Mac and controller need different addresses in the same subnet.

In **System Settings -> Network**, select the USB Ethernet adapter and use:

- Configure IPv4: **Manually**
- IP address: `192.168.1.200`
- Subnet mask: `255.255.255.0`
- Router: leave blank
- DNS: leave blank

If the controller itself ends in `.200`, choose another unused number between
2 and 254. If Wi-Fi also uses `192.168.1.x`, turn Wi-Fi off for the direct
wired test to avoid routing the connection through the wrong adapter.

## 3. Install the SDK

From this directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## 4. Run the read-only check

Replace the example address with the IP printed on the control box:

```bash
python check_xarm.py 192.168.1.123
```

Success ends with:

```text
PASS: controller reached; no motion commands were sent.
```

For structured output:

```bash
python check_xarm.py 192.168.1.123 --json
```

## 5. Open UFACTORY Studio

Once the read-only check passes, open the following address in a browser,
replacing the IP with the controller's address:

```text
http://192.168.1.123:18333
```

The Studio server is already installed in the xArm control box. Do not click
**Enable**, **Home**, **Initial Position**, **Play**, or a jog control until the
mounting orientation, payload, tool geometry, collision sensitivity, and full
workspace clearance have been verified.

## References

- [UFACTORY xArm Python SDK](https://github.com/xArm-Developer/xArm-Python-SDK)
- [UFACTORY connection troubleshooting](https://docs.supportarticle.ufactory.cc/support_articles/hardware/how-to-solve-server-is-not-ready.html)
