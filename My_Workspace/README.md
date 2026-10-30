# Bundu Restaurant

A comprehensive restaurant ordering and management system built with Flask and vanilla JavaScript.

## Setup Instructions

### 1. Environment Configuration

Before running the application, you need to set up your environment variables:

```powershell
Copy-Item .env.example .env
```

For local UI development, keep `BUNDU_ENV=development` from the example; staff and manager registration will not require invitation keys. The frontend lets you choose which account type to create, and there is no limit on the number of manager accounts. For production, set `BUNDU_ENV=production` and generate independent random values for `BUNDU_JWT_SECRET`, `BUNDU_MANAGER_REGISTRATION_KEY`, and `BUNDU_WAITER_REGISTRATION_KEY` (for example, with Python's `secrets.token_urlsafe(48)`) before deploying. Replace the placeholders in `.env` with those values. Production registration requires the applicable role-specific invitation key. Keep `.env` private and never put these values in frontend code.

```
BUNDU_ENV=development
BUNDU_JWT_SECRET=<random-secret-at-least-32-bytes>
BUNDU_MANAGER_REGISTRATION_KEY=<different-random-secret>
BUNDU_WAITER_REGISTRATION_KEY=<another-different-random-secret>
BUNDU_ALLOWED_ORIGINS=http://127.0.0.1:5500,http://localhost:5500
BUNDU_RATE_LIMIT_STORAGE_URI=memory://
```

`BUNDU_ALLOWED_ORIGINS` is a comma-separated list of exact frontend origins; set it to your deployed HTTPS origin(s) in production. HTTP is accepted only for localhost during development. The in-memory rate-limit store is suitable for local development and a single process. Configure a shared store such as Redis with `BUNDU_RATE_LIMIT_STORAGE_URI` when deploying multiple workers.

### Complaint response email (SMTP)

The manager can save a complaint response without sending it, then send it from the manager page. Configure `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, and `SMTP_USE_SSL` in the root `.env`; these values are only read by the backend and must never be added to frontend code. `SMTP_FROM` is optional and defaults to the authenticated `SMTP_USER` mailbox. The root `.env` is the only runtime environment file; `.env.example` is a safe template, not another active configuration. Gmail normally uses `smtp.gmail.com`, port `587`, and `SMTP_USE_SSL=false`; use a Google App Password (with 2-Step Verification enabled), not your account password. Outlook/Microsoft 365 normally uses `smtp.office365.com`, port `587`, and `SMTP_USE_SSL=false`; the mailbox must be permitted to use authenticated SMTP by its organization. For implicit TLS on port `465`, set `SMTP_USE_SSL=true`.

Complaint delivery state is added to the existing SQLite database automatically when the backend starts. A failed delivery is recorded and can be retried from the manager page. A response that was successfully sent cannot be edited or resent through the current interface. If provider SMTP is unreliable or unavailable, a transactional email API such as Resend or SendGrid is a recommended alternative; it would require replacing the backend SMTP transport with that provider's server-side API.

### 2. Install Dependencies

```powershell
.\.venv\Scripts\python.exe -m pip install -r Backend\requirements.txt
```

### 3. Run the Application

```powershell
.\.venv\Scripts\python.exe Backend\app.py
```

The application loads the root `.env` file and starts with debug mode disabled on `http://127.0.0.1:5000`. For deployment, use a production WSGI server behind HTTPS rather than Flask's development server. Set the production frontend origin in `BUNDU_ALLOWED_ORIGINS`, use a shared rate-limit store, and configure the reverse proxy to send HTTPS security headers. The static frontend's Content Security Policy is in its HTML; update `connect-src` if the API host changes.

Open `My_Workspace/` as the VS Code workspace. In a terminal, run `npm install` from `FrontEnd/`, then start the public site with `npm run dev` from that folder. Vite serves the React-powered home, menu, and DineDesk Kitchen at `http://127.0.0.1:5500`; start the Flask API separately as described above. Build the frontend for deployment with `npm run build` from `FrontEnd/`. The existing staff and account pages remain part of the same site.

Guests can place an order and then reserve a table for a future half-hour slot. Order-linked reservations hold a table for 60 minutes, include a separate R50 fee for the waiter to record as paid, and are checked against existing table bookings and active orders. Dine-in orders require a currently available table; the API rechecks availability inside the order transaction.

Waiter and manager sign-ins and sign-outs are recorded in SQLite. Managers can add chefs, waiters, and managers with individual hourly pay rates, and schedule staff for dated shifts. Waiters and managers receive sign-in accounts; chefs are roster-only. A scheduled staff member is marked absent after their shift ends if they have not signed in that day. The manager's **Shifts** section shows daily hours and monthly gross-pay estimates using each staff member's hourly rate, with overtime after 45 hours in a week at 1.5×. These are gross estimates and do not include tax or other deductions. Attendance records are retained for reports and manager-only exports. The **Exports** section downloads orders, pantry and stock movements, staff and shifts, complaints, reservations, waitlist entries, or all company records as PDF documents.

## Project Structure

- **Backend/** - Flask API server with order management, authentication, and complaints system
- **FrontEnd/** - React/Vite public home, menu, and ordering experience, with existing staff and account pages
- **.vscode/settings.json** - Prevents Live Server from serving environment files, backend files, or the virtual environment
- **.env.example** - Template for environment configuration (copy to `.env` before running)
- **.gitignore** - Excludes environment files, SQLite databases and backups, uploaded images, the virtual environment, and Python caches
