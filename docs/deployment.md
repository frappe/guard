# Deploy Guard

This guide puts Guard on a production site and connects it to GitHub with a GitHub App. GitHub sends webhooks to the site, and Guard posts reviews as the App.

## Before you start

You need:

- A Frappe v16 site that GitHub can reach on a public HTTPS URL. In this guide, the URL is `https://guard.example.com`.
- An AI gateway URL and key. Guard uses [Grove](https://github.com/frappe/grove) or any OpenAI-compatible or Anthropic-compatible endpoint.
- Owner access to the GitHub organization or account that will own the App.

The server must have:

| Requirement | Why |
| - | - |
| Python 3.14 or later | Guard requires it. |
| The `git` command | Guard keeps a mirror of each repository. |
| Persistent disk | The mirrors are in `sites/<site>/private/guard/git/`. |
| Workers for the `short`, `default` and `long` queues | Reviews and indexing run on `long`. |
| The scheduler | Guard polls reactions and runs maintenance jobs. |
| MariaDB 11.7 or later (recommended) | Native vector search. Older versions use a slower fallback. |
| `podman` (optional) | Only for runtime validation, which runs tests in a container. |

## 1. Install the app

```bash
bench get-app https://github.com/<owner>/guard --branch develop
bench --site guard.example.com install-app guard
```

On a hosted platform, add the app to the bench and install it on the site from the dashboard.

## 2. Connect the AI gateway

1. In Desk, open **Guard Settings**.
2. Set **Gateway URL**. Do not add `/v1`. Guard adds it.
3. Set **Gateway API Key** and **Dialect**.
4. Click **Fetch models**.
5. Set **Base Model**, **Plus Model**, **Apex Model** and **Fast Model**.
6. Set **Embedding Model** if the gateway has one. If it does not, leave it empty. Guard then uses a simpler local fallback for search.
7. Set **Public URL** to `https://guard.example.com`. Links in review comments use this URL.
8. Save.

## 3. Create the GitHub App

1. Open the settings of the organization or account that will own the App. Go to **Developer settings**, then **GitHub Apps**, then **New GitHub App**.
2. Set a name, for example `acme-guard`, and a homepage URL.
3. Make a webhook secret:

   ```bash
   openssl rand -hex 24
   ```

4. Under **Webhook**, select **Active** and set:
   - **Webhook URL**: `https://guard.example.com/api/method/guard.webhooks.endpoint.github_webhook?provider=GitHub`
   - **Secret**: the value from step 3.
5. Set the repository permissions:

   | Permission | Access |
   | - | - |
   | Checks | Read and write |
   | Commit statuses | Read and write |
   | Contents | Read and write |
   | Issues | Read and write |
   | Pull requests | Read and write |
   | Metadata | Read |

   Guard does not push code. GitHub requires Contents write access to resolve review threads.

6. Set the organization permission **Members** to **Read**.
7. Subscribe to these events:
   - Check run
   - Issue comment
   - Pull request
   - Pull request review
   - Pull request review comment
   - Push
8. Under **Where can this GitHub App be installed?**, select **Only on this account**.
9. Click **Create GitHub App**.
10. Record the **App ID** and the slug. The slug is the last part of the App URL.
11. Click **Generate a private key**. GitHub downloads a `.pem` file.

## 4. Add the Code Provider

1. In Desk, create a **Code Provider**.
2. Set these fields:

   | Field | Value |
   | - | - |
   | Title | `GitHub` |
   | Provider Type | GitHub |
   | Auth Mode | App |
   | App ID | The App ID from step 3 |
   | App Slug | The slug from step 3 |
   | Private Key | The full contents of the `.pem` file |
   | Webhook Secret | The secret from step 3 |

3. Save.
4. Click **Test connection**. The result must be "Connected as" and the App slug.
5. Delete the `.pem` file from your computer. Guard keeps the key encrypted.

The webhook URL names the Code Provider: `provider=<title>`. The form shows the full URL in **Webhook URL**. Copy it from there into the GitHub App, so the two always match.

## 5. Install the App on repositories

1. Open `https://github.com/apps/<slug>/installations/new`.
2. Select the account, then the repositories.
3. Click **Install**.

GitHub sends an installation webhook. Guard then:

- creates a **Guard Team** for the account;
- creates a **Code Repository** for each repository;
- enables reviews and starts indexing, when **Auto Enable New Repositories** is on.

To add repositories later, change the installation on GitHub. Guard receives the change.

## 6. Check the setup

1. In Desk, open **Webhook Delivery**. The installation delivery must have the status Processed.
2. Open **Code Repository**. Each repository must show the index status Indexed after some minutes.
3. Open a test pull request.
4. Make sure that Guard:
   - adds an 👀 reaction;
   - posts a summary comment with a confidence score;
   - sets a check named Guard.

If a delivery has the status Failed, open it to see the error. If no delivery arrives, open the App on GitHub, go to **Advanced**, and look at **Recent Deliveries**:

- A 401 response means that the webhook secret on the App and in the Code Provider are different.
- A 404 or 502 response means that the webhook URL is wrong.

If the installation webhook did not arrive, click **Sync repositories** on the Code Provider. Guard reads the installations from GitHub.

## Default behavior

| Setting | Default |
| - | - |
| Reviews | On pull request open and on each push |
| Re-reviews | Post only P0 and P1 findings |
| Earlier findings | Guard resolves a thread when the code fixes the finding, or when the finding no longer applies |
| Guard check | Passes only at 5/5 with no open findings |
| Approval | Guard approves at 5/5 with no open findings. A new push dismisses the approval. |
| Summary sections | Collapsed |

To change these settings, open **Review Settings** in Desk, or add `.guard/config.toml` to a repository. Refer to [Configuration](spec/06-configuration.md).

To stop Guard from approving pull requests, clear **Auto Approve** in **Review Settings**.

## Rotate secrets

- **Webhook secret**: make a new value. Set it on the GitHub App and in the Code Provider at the same time.
- **Private key**: generate a new key on the GitHub App. Paste it into the Code Provider. Then delete the old key on GitHub.
- **Gateway key**: set the new key in Guard Settings. Then revoke the old key in the gateway.
