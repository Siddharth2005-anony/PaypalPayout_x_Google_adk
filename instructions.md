# GitHub push instructions

Run these commands from the project root in Git Bash.

## First push

```bash
git init
git branch -M main
git remote add origin https://github.com/<USERNAME>/<REPOSITORY>.git
git add .
git diff --cached --check
git commit -m "Document PayPal payout agent"
git push -u origin main
```

Replace `<USERNAME>` and `<REPOSITORY>` with the empty GitHub repository details. If `origin` already exists, update it instead:

```bash
git remote set-url origin https://github.com/<USERNAME>/<REPOSITORY>.git
```

## Later pushes

```bash
git add .
git diff --cached --check
git commit -m "Describe the change"
git push
```

Before committing, check the staged file list:

```bash
git status
```

Do not stage `.env` or any file containing API keys, PayPal secrets, SMTP passwords, or real recipient data. The repository `.gitignore` excludes local `.env` files and common Python-generated files.
