Domain Agent — Guide de déploiement
Structure du projet
```
domain-agent/
├── .github/workflows/scan.yml   ← Cron GitHub Actions (3x/jour)
├── agent/
│   ├── agent.py                 ← Script principal
│   └── requirements.txt         ← Dépendances Python
└── dashboard/
    └── index.html               ← Dashboard web (ouvrir dans navigateur)
```
---
Étape 1 — Google Sheets API
Va sur https://console.cloud.google.com
Crée un projet → "Domain Agent"
Active Google Sheets API + Google Drive API
IAM & Admin → Comptes de service → Créer un compte
Télécharge le fichier JSON des clés
Crée un Google Sheet nommé "Domain Agent"
Partage ce Sheet avec l'email du compte de service (Éditeur)
---
Étape 2 — Gmail App Password
Google Account → Sécurité → Validation en 2 étapes (activer si besoin)
Recherche "Mots de passe des applications"
Génère un mot de passe → Nom : "Domain Agent"
Garde ce mot de passe 16 caractères
---
Étape 3 — OpenPageRank (gratuit)
https://www.domcop.com/openpagerank/signup
Inscription gratuite → Récupère ta clé API
Limite : 100 domaines/jour (suffisant pour phase 1)
---
Étape 4 — GitHub Secrets
Dans ton repo GitHub → Settings → Secrets and variables → Actions :
Secret	Valeur
`GOOGLE_SHEET_NAME`	`Domain Agent`
`GCP_SERVICE_ACCOUNT_JSON`	Contenu complet du fichier JSON téléchargé
`GMAIL_FROM`	ton.email@gmail.com
`GMAIL_TO`	ton.email@gmail.com
`GMAIL_APP_PASSWORD`	Le mot de passe 16 caractères
`OPR_API_KEY`	Ta clé OpenPageRank
---
Étape 5 — Push du code sur GitHub
```bash
git init
git add .
git commit -m "Initial domain agent"
git branch -M main
git remote add origin https://github.com/TON_USERNAME/domain-agent.git
git push -u origin main
```
---
Étape 6 — Connecter le dashboard
Ouvre ton Google Sheet "Domain Agent"
Fichier → Partager → Publier sur le web
Sélectionne la feuille "opportunites" → Format CSV → Publier
Copie le lien généré
Dans `dashboard/index.html`, remplace `REMPLACE_PAR_TON_URL_CSV_GOOGLE_SHEETS`
---
Test manuel
Dans GitHub → Actions → "Domain Agent Scan" → Run workflow
---
Planning des scans automatiques
Heure Paris	Cron UTC
07h00	0 6 * * *
13h00	0 11 * * *
19h00	0 17 * * *
---
Seuil d'alerte email
Modifie `SCORE_ALERT = 80` dans `agent/agent.py` selon ta sensibilité.
---
Phase 2 — Évolutions prévues
[ ] Backtest scoring sur historique Namebio
[ ] API GoDaddy Auctions (enchères en temps réel)
[ ] Ahrefs API (backlinks précis)
[ ] Notification push mobile
[ ] Semi-automatisation achat (score > 90)
