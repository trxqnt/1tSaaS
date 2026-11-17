# Guide de lancement — pas à pas, sans connaissances techniques

Objectif : mettre Relancio en ligne en un week-end, puis aller chercher les premiers clients payants.
Les prix et offres gratuites des services cités changent : vérifiez-les sur leurs sites avant de vous engager.

## Ce que fait le produit (en une phrase)

Un freelance ou une TPE ajoute ses factures impayées ; Relancio envoie à sa place 4 relances graduées
(J+1, J+8, J+20, J+35 après l'échéance), calcule les pénalités de retard et l'indemnité de 40 €, et s'arrête
dès que la facture est marquée payée. Gratuit jusqu'à 3 factures impayées, puis abonnement Pro à 29 €/mois.

## Avant de commencer : 4 choses à avoir

1. **Un statut pour encaisser en France.** Le plus simple : micro-entreprise (démarches sur le Guichet unique
   de l'INPI, gratuit). Vous obtenez un numéro SIRET, à mettre dans les mentions légales.
2. **Un nom de domaine** (≈ 10 €/an) chez OVH, Gandi, Namecheap… Vérifiez que le nom « Relancio » (ou celui que
   vous choisirez) est libre comme domaine et comme marque (recherche gratuite sur le site de l'INPI).
   Vous pouvez changer le nom en modifiant la variable `APP_NAME`.
3. **Un compte Stripe** pour encaisser les abonnements.
4. **Un compte d'envoi d'e-mails** (Brevo, Resend, Mailjet…) pour que les relances partent vraiment.

## Étape 1 — Héberger l'application

Il faut un hébergeur qui garde un **disque permanent** (l'application stocke ses données dans un fichier).
Options courantes : **Render** (service web payant + disque persistant) ou **Railway** (volume persistant).

1. Créez un compte, puis un nouveau « Web Service » à partir du dossier `relancio` (déposez-le sur un dépôt GitHub
   privé, ou utilisez l'envoi de fichiers de l'hébergeur).
2. Commande d'installation : `pip install -r requirements.txt`
   Commande de démarrage : `gunicorn app:app --workers 2 --bind 0.0.0.0:$PORT`
3. Ajoutez un **disque / volume** monté sur `/data`, et mettez la variable `DATABASE=/data/relancio.db`.
4. Ajoutez toutes les variables du fichier `.env.example` (voir étapes suivantes pour les valeurs).
   Pour `SECRET_KEY` et `CRON_TOKEN`, générez deux longues chaînes aléatoires (un gestionnaire de mots de passe
   sait le faire).
5. Reliez votre nom de domaine à l'application (l'hébergeur indique les enregistrements DNS à créer),
   puis mettez `BASE_URL=https://votre-domaine.fr`. Le HTTPS est fourni automatiquement.
6. Ouvrez `https://votre-domaine.fr/health` : vous devez voir `{"status":"ok"}`.

## Étape 2 — Faire partir les e-mails

1. Chez Brevo / Resend / Mailjet : ajoutez votre domaine et créez les enregistrements DNS demandés (SPF, DKIM).
   **Sans cela, les relances finissent en spam.**
2. Récupérez les identifiants SMTP et renseignez `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`.
3. `MAIL_FROM` doit être une adresse de **votre domaine vérifié**, par exemple `relances@votre-domaine.fr`.
4. Test : créez un compte, ajoutez une facture avec **votre propre e-mail** comme client et une échéance dépassée
   de 2 jours, puis cliquez sur « Envoyer la relance maintenant ». Vérifiez la réception (et le dossier spam).

Tant que `SMTP_HOST` est vide, l'application est en **mode simulation** : elle enregistre les relances sans
rien envoyer. C'est volontaire, pour tester sans risque.

## Étape 3 — Déclencher les relances chaque jour

Les relances sont envoyées quand quelqu'un « réveille » l'application une fois par jour.

1. Créez un compte gratuit sur un service de tâches planifiées (par exemple cron-job.org).
2. Créez une tâche quotidienne (par exemple à 9 h) qui appelle l'adresse :
   `https://votre-domaine.fr/cron/run`
   en ajoutant un en-tête HTTP `X-Cron-Token` égal à votre `CRON_TOKEN`.
   (Si le service ne permet pas les en-têtes : `https://votre-domaine.fr/cron/run?token=VOTRE_CRON_TOKEN`.)
3. Une réponse du type `{"checked": 12, "sent": 2, ...}` signifie que ça marche.

## Étape 4 — Encaisser avec Stripe

1. Dans Stripe : créez un produit « Relancio Pro », prix récurrent de 29 € par mois.
2. Créez un **Payment Link** pour ce prix → collez-le dans `STRIPE_PAYMENT_LINK`.
3. Activez le **portail client** (Réglages → Facturation → Portail client) → collez le lien de connexion dans
   `STRIPE_PORTAL_LINK`. Vos clients pourront résilier seuls.
4. Créez un **webhook** vers `https://votre-domaine.fr/webhooks/stripe` pour les événements
   `checkout.session.completed` et `customer.subscription.deleted`. Collez la clé de signature (`whsec_…`)
   dans `STRIPE_WEBHOOK_SECRET`.
5. Test en **mode test de Stripe** avec une carte de test : le compte doit passer en Pro automatiquement
   après paiement, et repasser en gratuit à la résiliation.
   Pour passer un compte en Pro à la main : `python app.py set-plan email@client.fr pro`.

## Étape 5 — Compléter les pages légales (obligatoire)

Ouvrez `/mentions-legales`, `/cgu` et `/confidentialite` : les passages entre crochets `[...]` sont à remplacer par
vos informations réelles (dans `app.py`, section « legal.html »). Ces textes sont des modèles de départ, pas un
avis juridique : faites-les relire avant de facturer des clients.

Points à connaître :
- Vos clients saisissent des données personnelles de **leurs** débiteurs : vous êtes sous-traitant au sens du RGPD.
  Hébergez de préférence en Europe et tenez un petit registre de traitement (modèle gratuit sur cnil.fr).
- Les pénalités de retard ne sont réclamables que si elles figurent sur les factures / CGV du client. Le taux par
  défaut (8,25 %) correspond à 3 fois le taux d'intérêt légal du second semestre 2026 (2,75 %) ; il se modifie dans
  les réglages et change chaque semestre.
- Le mail n° 4 est une dernière relance amiable, **pas** une mise en demeure formelle (qui s'envoie en
  recommandé). Dites-le clairement à vos clients.

## Étape 6 — Check-list avant d'ouvrir au public

- [ ] `/health` répond OK
- [ ] Inscription, ajout de facture, relance manuelle : testés avec votre propre e-mail
- [ ] Relance reçue **hors spam**
- [ ] Tâche quotidienne créée et testée
- [ ] Paiement test Stripe → compte Pro ; résiliation → compte gratuit
- [ ] Pages légales complétées
- [ ] Sauvegarde : demandez à l'hébergeur les sauvegardes du disque, ou téléchargez `relancio.db` chaque semaine

## Coûts mensuels typiques (à vérifier)

Hébergement avec disque : quelques euros à une vingtaine d'euros. E-mails : gratuit au démarrage chez plusieurs
fournisseurs. Domaine : ≈ 1 €/mois. Stripe : un pourcentage + un montant fixe par paiement.

## Limites connues de cette version

Base SQLite (largement suffisante jusqu'à plusieurs centaines de comptes), pas de réinitialisation de mot de passe
par e-mail, pas d'import CSV, pas d'envoi de SMS. Ce sont les prochaines améliorations naturelles une fois les
premiers clients là : dites-moi lesquelles ils demandent et je les ajoute.
