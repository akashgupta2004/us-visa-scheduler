# Running the Scheduler on Two Different PCs (Native Webhook Method)

To optimize performance and avoid rate limits, you can split the bot's workload across two different computers using **Tailscale** and native Git. This method completely avoids Windows File Sharing permissions and allows you to just use `git pull` on both machines!

1. **Polling Laptop (PC 1):** Dedicated to polling for available slots and running `POLLING_ONLY` accounts.
2. **Booking Laptop (PC 2):** Dedicated to receiving slot triggers (via webhook) and executing the booking for `RESERVED_BOOKING` accounts.

---

## Step 1: Install Tailscale & Git
1. Download and install [Tailscale](https://tailscale.com/) on both PC 1 and PC 2, logging into the same account.
2. Open the Tailscale app on **PC 2 (Booking Laptop)** and note down its Tailscale IP address (e.g., `100.90.86.48`).
3. Ensure the project is cloned via Git on **both laptops** in their own separate folders. Any time you make code updates, you just run `git pull` on both machines.

---

## Step 2: Configure the Booking Laptop (PC 2)
Because the state files (`state_{uid}.json`) are no longer shared over the network, PC 2 needs a way to receive trigger instructions from PC 1. The orchestrator automatically launches a hidden **Webhook Receiver** on port `5000` when set to `BOOKING` mode.

1. Open a terminal and navigate to your local `us-visa-scheduler` folder.
2. Open the `.env` file and set the laptop role:
   ```env
   LAPTOP_ROLE=BOOKING
   ```
3. Run the orchestrator:
   ```bash
   python main.py
   ```
   *Note: You will see a log message saying `Webhook Receiver listening on port 5000`. The laptop will now sit quietly and wait for incoming triggers over your Tailscale network!*

---

## Step 3: Configure the Polling Laptop (PC 1)
Now you need to tell PC 1 where to send the triggers when it finds a slot.

1. Open a terminal and navigate to your local `us-visa-scheduler` folder on PC 1.
2. Open the `.env` file and set **both** variables:
   ```env
   LAPTOP_ROLE=POLLING
   REMOTE_TRIGGER_URL=http://<PC2_TAILSCALE_IP>:5000
   ```
   *(For example: `REMOTE_TRIGGER_URL=http://100.90.86.48:5000`)*

3. Run the orchestrator:
   ```bash
   python main.py
   ```
   *Note: Because you provided a `REMOTE_TRIGGER_URL`, every time PC 1 finds a slot, it instantly fires a POST request over Tailscale to PC 2's port 5000. PC 2 receives it, saves the trigger locally, and snipes the slot!*

---

## Step 4: Testing the 1-Second Trigger Gap
When multiple `RESERVED_BOOKING` accounts are qualified at the exact same time, the Polling Laptop is programmed to queue their triggers with exactly a 1.0-second gap to prevent concurrent rate limits. 

You can test this by running the test script:
```bash
python src/test_trigger.py
```
*(You should see all accounts trigger sequentially with a Gap >= 1.0 seconds).*
