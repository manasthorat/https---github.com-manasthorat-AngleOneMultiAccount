# Angle One Multi-Account Trading System

A web-based platform to automate and monitor trading across multiple Angle One accounts, with TradingView webhook integration, real-time position tracking, and multi-account management.


![image](https://github.com/user-attachments/assets/f6960fcd-684d-4340-872e-caf7ada32376)
![image](https://github.com/user-attachments/assets/f3e48b1e-e282-4786-94b8-494a54851367)
![image](https://github.com/user-attachments/assets/2bd99a75-0e12-417a-96a7-ae564bee21b7)
![image](https://github.com/user-attachments/assets/9f46cc96-5293-42ef-8ecb-38c178348753)
![image](https://github.com/user-attachments/assets/32afe19c-7063-4dcc-8386-b9a481114807)

## Features

- **Multi-Account Management:** Add, edit, and monitor multiple Angle One trading accounts.
- **Options Chain Viewer:** Visualize option chains, generate webhook templates for TradingView, and download ready-to-use JSON templates.
- **Automated Trading:** Execute trades automatically based on TradingView webhook alerts.
- **Real-Time Dashboard:** Monitor account balances, open positions, P&L, and system health in a unified dashboard.
- **Order & Position Tracking:** View and manage orders and positions across all linked accounts.
- **Web-Based UI:** Responsive interface built with Bootstrap, jQuery, and Chart.js.
- **Notifications:** In-app notifications for important events and system alerts.

## Project Structure

```
.
├── account_manager.py
├── accounts.json
├── allaccounts.json
├── angel_websocket_manager.py
├── angle-one-trading-ui.py
├── completed_option_trades.json
├── config.json
├── monitor_config.json
├── option_trades.json
├── options_module.py
├── options_trade_manager.py
├── requirements.txt
├── trade_monitor_service.py
├── trading_app.log
├── static/
│   ├── css/
│   │   └── main.css
│   └── js/
│       ├── common.js
│       └── dashboard.js
├── templates/
│   ├── base.html
│   ├── dashboard.html
│   ├── accounts.html
│   ├── add_account.html
│   ├── edit_account.html
│   ├── positions.html
│   ├── orders.html
│   ├── option_chain_viewer.html
│   ├── webhook_generator.html
│   └── ...
└── logs/
```

## Getting Started

### Prerequisites

- Python 3.10+
- pip

### Installation

1. **Clone the repository:**
   ```sh
   git clone <repo-url>
   cd <project-folder>
   ```

2. **Install dependencies:**
   ```sh
   pip install -r requirements.txt
   ```

3. **Configure your accounts:**
   - Add your Angle One account details in `accounts.json` or via the web UI.

4. **Set up configuration:**
   - Edit `config.json` and `monitor_config.json` as needed.

### Running the Application

```sh
python angle-one-trading-ui.py
```

- The web UI will be available at [http://localhost:5000](http://localhost:5000) by default.

### Usage

- **Login:** Use the default credentials (`admin/admin`) or your configured admin account.
- **Accounts:** Add and manage Angle One accounts from the "Accounts" section.
- **Dashboard:** Monitor balances, positions, and system status.
- **Options Chain:** View option chains and generate TradingView webhook templates.
- **Orders & Positions:** Track and manage all orders and open positions.

### Webhook Integration

- Use the "Webhook Generator" or "Option Chain Viewer" to generate TradingView-compatible webhook JSON templates.
- Configure TradingView alerts to POST to your server's webhook endpoint.

## Contributing

Pull requests are welcome! For major changes, please open an issue first to discuss what you would like to change.

## License

This project is for educational and personal use only. See [LICENSE](LICENSE) for details.

---

**Note:** This project is not affiliated with Angle One. Use at your own risk.
