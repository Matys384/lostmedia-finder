# Reprise du projet — état au 21 octobre 2026

**Lire ce fichier avant de relancer quoi que ce soit.** L'utilisateur demande maintenant une transmission à un autre agent, pas une poursuite de l'exploration. Aucun worker d'analyse ou de découverte n'est encore en cours au moment du relevé. Deux serveurs web locaux restent ouverts.

## Contraintes utilisateur qui font foi

1. Construire un véritable outil Python open source, tester réellement, ne pas annoncer une fonction comme validée sur la seule base de son implémentation.
2. Référence BSS fournie et reconnue par l'utilisateur : <https://www.youtube.com/watch?v=NndwzzpMunQ>. Utiliser ces images comme référence. Le segment de validation est 30–42 s.
3. Départ chronologique **15/08/2007**, puis jusqu'à fin 2012 ultérieurement. Cependant, **la campagne active est strictement limitée au 15–31 août 2007**. Ne pas lancer septembre 2007, 2008 ou les années suivantes avant validation et nouvelle instruction.
4. Maximiser la découverte publique, y compris hors Mario : intervalles, API, chaînes/playlists et catalogues. Fin de pagination ≠ couverture exhaustive.
5. Garder **FP16 par défaut**. Budget 500 Gio, conservation des références et empreintes, suppression des téléchargements après analyse, reprise persistante.
6. Ne pas contourner quotas, restrictions, refus d'accès ou vidéos privées. La clé YouTube fournie est déjà configurée dans `.env` (ignoré par Git). **Ne pas la recopier dans la documentation, les logs, les réponses ou le dépôt.**
7. L'utilisateur veut des compteurs exacts et des limites explicites. Les résultats contrôlés ne prouvent pas la précision sur tout le Web.

## Localisation et services

- Projet : `.`.
- Python installé : `.venv\Scripts\python.exe`, Python 3.11.9.
- CLI : `.venv\Scripts\lostmedia.exe` ; import éditable installé.
- Matériel réellement utilisé : RTX 5090 32 Go ; PyTorch 2.11.0+cu128, torchvision 0.26.0+cu128 ; inférence CUDA testée. FAISS est **CPU**, pas GPU.
- FFmpeg et ffprobe sont déjà dans PATH.
- Interface de campagne : <http://127.0.0.1:8765>, PID observé **34960**, racine `data/chronological`.
- Interface des preuves BSS contrôlées : <http://127.0.0.1:8766>, PID observé **25688**. Racine exacte dans `data/validation/report-dinov2.json`, champ `data_root`.
- Ces PID sont des observations, pas des identifiants permanents. Les serveurs ont été lancés avant certaines dernières modifications Python ; **redémarrer les serveurs après vérification de leurs lignes de commande** pour charger tout le code actuel. Les fichiers statiques sont lus depuis disque.
- Aucun service Windows ni démarrage automatique après reboot n'a été installé. Reprise = relancer la CLI, avec récupération des baux expirés.
- Git a été initialisé localement. **Pas de commit et pas de remote** ; les sources sont encore non suivies. Aucun dépôt n'a été publié.

## État exact de la campagne 15–31 août 2007

Source de vérité au passage de relais : [`reports/final-audit.json`](reports/final-audit.json), généré après la fin des traitements.

| Source | Entrées uniques découvertes | Accès décodable confirmé | Analysées | Ignorées par limites | Inaccessibles | En échec | En attente |
|---|---:|---:|---:|---:|---:|---:|---:|
| YouTube | 1 260 | 14 | 14 | 0 | 1 | 0 | 1 245 |
| Dailymotion | 6 600 | 48 | 48 | 2 | 0 | 0 | 6 550 |
| Internet Archive | 2 115 | 5 | 5 | 17 | 0 | 0 | 2 093 |
| **Total** | **9 975** | **67** | **67** | **19** | **1** | **0** | **9 888** |

**Aucun job `running`.** L'accessibilité des 9 888 entrées en attente n'est pas connue. « Accès confirmé » est une borne observée, pas le nombre total de vidéos accessibles. Les limites du test d'analyse étaient **100 Mio par téléchargement et 600 s de durée** ; elles sont plus strictes que les valeurs générales de `config.toml` (2 Gio / 2 h). Les ignorées ne sont pas des vidéos prouvées inaccessibles.

Internet Archive : les 2 115 sont des **items de catalogue `mediatype:movies`**, pas 2 115 fichiers vidéo distincts certifiés. Filtre `publicdate`, pas `date`. L'API Advanced Search annonçait 2 115 items pour cette requête à cet instant ; cela ne prouve ni l'exhaustivité de l'Archive ni celle des vidéos de 2007. Un fichier compatible est analysé par item, l'expansion complète des fichiers manque encore.

Frontières : Archive 1 fenêtre `exhausted` ; Dailymotion 5 `pending`, 4 `saturated` quotidiennes, 2 parents `split` ; YouTube 19 `exhausted`, 11 `pending`. Seule la période `id=1`, 15–31 août 2007, est `active`. Les 64 autres mois préparés sont encore `pending`.

Il y a **0 séquence BSS vérifiée dans le corpus réel**. Un candidat Dailymotion faible est conservé (`video_id=118`, score ≈0,588, une seule image, 0 frame temporelle validée). Ne pas l'annoncer comme une découverte de BSS.

## Doublons : ne pas masquer la limite des anciens journaux

Les clés `(source, external_id, is_reference)` dédupliquent bien les vidéos. Cependant, les premiers événements enregistrent seulement `returned` et `new` : **leur différence mélange les retours répétés et les éléments filtrés par date**. Ne pas appeler tous ces écarts « doublons exacts ».

- Archive : 3 715 retours, 2 115 nouveaux ; le connecteur initial répétait les pages, ce qui explique les répétitions observées. Le connecteur a été remplacé et retesté.
- Dailymotion : 6 600 retours et 6 600 nouveaux dans les événements enregistrés.
- YouTube : 1 514 retours et 1 260 nouveaux, toutes méthodes incluses. Le lot playlist inclut des résultats hors période ; **254 n'est donc pas un compte certifié de doublons**.
- La table `discovery_quality`, ajoutée ensuite, sépare explicitement doublons et filtrés. Son sous-audit mesuré : **500 retours YouTube, 433 nouveaux, 67 doublons, 0 hors période**. Il ne représente pas rétroactivement tous les appels précédents.
- Le script `youtube_audit.py` n'utilise pas encore le compteur `filtered` dans son appel à `record` : corriger cette instrumentation avant de réutiliser le script sur une requête qui renverrait des dates hors fenêtre. La boucle de campagne, elle, le transmet.

## Connecteurs réellement vérifiés

### YouTube

- Clé API fonctionnelle, `search.list` testé : recherche sans mots-clés mensuelle, 17 recherches quotidiennes, 12 thèmes généraux (`music`, `funny`, `tutorial`, `gameplay`, `vlog`, `news`, `animation`, `sports`, `concert`, `dance`, `interview`, `documentary`), puis pagination supplémentaire.
- Une chaîne réelle a été explorée via `channels.list` → playlist d'uploads → `playlistItems.list` : 47 résultats lus, 6 nouvelles vidéos dans la période. Voir `reports/youtube-channel-integration.json`. Cela ne couvre pas tout l'historique de la chaîne.
- 15 téléchargements/analyse tentés, 14 réussis, 1 vidéo déclarée indisponible (`6ozi4os5KFs`). Le message a ensuite été correctement classé `inaccessible`.
- Compteur local final : **48 appels API YouTube le 21/10/2026 UTC**, plafond local 80. Ce compteur est conservateur et compte aussi les appels non-Search ; ce n'est pas le tableau de quota officiel Google.
- Le téléchargement yt-dlp fonctionne sans runtime JS sur les vidéos testées mais émet un avertissement de formats potentiellement manquants. Ne pas affirmer que tous les formats ou tous les liens fonctionnent.
- Une répétition de la recherche mensuelle a renvoyé une liste différente : `reports/youtube-pagination-check.json`. Les snapshots changent ; ne pas interpréter `totalResults` ni l'absence de page suivante comme un inventaire exhaustif.

### Dailymotion

- API publique `/videos` réellement testée sans mot-clé, avec `created_after` et `created_before`, pagination et subdivision.
- Un paramètre `search` vide provoquait HTTP 400 : maintenant omis.
- Première tentative de téléchargement : échec pour dépendance `curl-cffi` manquante. Dépendance installée, ajoutée au projet, reprise effectuée.
- Lot de reprise : 8/10 analysées, 2 ignorées. Lot élargi : **40/40 analysées en 214,735 s**, soit environ **11,18 vidéos/minute**, téléchargement inclus.
- Plafond de recherche observé à 1 000 résultats ; certaines fenêtres quotidiennes restent saturées. La subdivision date n'autorise aucune promesse de couverture complète.

### Internet Archive

- Le premier endpoint `services/search/v1/scrape` répétait un curseur/page identique dans ce test réel. Corrigé en utilisant `advancedsearch.php`, tri `identifier asc`, pagination vérifiée par des pages différentes.
- Limite prévue à 10 000 résultats triés : subdivision avant de la dépasser. Tests de contrat présents.
- 2 115 items parcourus sur cette requête. 22 tentatives d'analyse cumulées : 5 analysées, 17 ignorées parce qu'aucun fichier compatible ne satisfaisait les limites ou que la durée était trop grande.

## Mesures de détection et de compression

Test BSS : référence 12 s, insertion à **73 s** dans 180 s. DINOv2 FP16 retrouve 4/4 variantes : recadrage + compression, miroir, incrustation, vitesse ×1,25. Erreurs d'offset **0 / 0 / 0 / 0,25 s**. Aucun signal temporel validé sur le témoin BBB sans BSS. L'image la plus tôt appariée peut être à 75 s dans l'incrustation ; **l'offset estimé** reste 73 s : ne pas confondre début des preuves appariées et début de l'insertion.

- Nouvelle extraction FP16 : 5 vidéos traitées en **25,548 s**. Modèle déjà présent dans le cache Hugging Face ; embeddings recalculés dans une racine neuve.
- Première validation DINO FP32 : 45,401 s, incluant chargement initial. CLIP : 4/4 et 42,696 s. pHash seul : **3/4**, rate l'incrustation ; ne pas l'utiliser comme filtre d'élimination définitif.
- Les 4 positives et 1 témoin sont contrôlés, avec arrière-plan BBB répété. Les 67 vidéos anciennes élargissent le corpus de distracteurs ; elles ne constituent pas un jeu Web annoté représentatif.

Dernier benchmark : **34 874 vecteurs**, dimension 768, 84 requêtes, **67 vidéos réelles supplémentaires**. `reports/compression.json` fait foi.

| Méthode | Octets de vecteurs/index mesurés | Recouvrement top-10 vs FP32 | Inserts BSS retrouvés |
|---|---:|---:|---:|
| FP32 | 107 132 928 | 100 % | 4/4 |
| **FP16** | **53 566 464** | **99,88095 %** | **4/4** |
| int8 | 26 922 728 | 99,64286 % | 4/4 |
| PQ 96×8 | 4 134 422 | 81,90476 % | **3/4**, incrustation manquée |

Tailles FP32/16/int8 = payload des vecteurs ; taille PQ = index sérialisé avec codebooks. Les index Flat de comparaison remontent les vecteurs en FP32 en RAM. Les distracteurs réels avaient déjà été persistés en FP16 ; seules les extractions contrôlées initiales fournissent le FP32 original. PQ est entraîné sur le corpus candidat, références propres utilisées comme requêtes. **Le recouvrement top-10 n'est pas le rappel global de détection sur le Web.** Pas de justification pour remplacer FP16 par PQ en production.

Stockage de la racine de campagne au relevé : **44 073 616 octets (~42 Mio)**, dont 40 318 721 octets de fichiers d'empreintes pour les 67 vidéos. 28 574 vues de frames extraites. Ces tailles excluent les autres jeux de validation, `.venv` et le cache de modèles global. Les téléchargements réussis de cette racine ont été supprimés ; les fichiers sources de validation fournis/localement générés sont conservés.

Les temps `processing_metrics.seconds` ne doivent pas être additionnés pour déduire un débit réseau global : le préchargement est asynchrone et une ancienne version mesurait moins d'étapes. Utiliser les temps muraux des rapports de lots pour le débit. Plusieurs audits se sont chevauchés pendant le développement : refaire un benchmark isolé avant toute comparaison matérielle stricte.

## Tests et état du code

Dernière suite : **17 tests réussis**, `reports/tests.txt`. `pip check` réussi. UI observée dans le navigateur : compteurs chargés et 4 cartes BSS avec miniatures/horodatages sur 8766. Les tout derniers changements de présentation des tableaux n'ont pas été revérifiés visuellement.

Tests : claims concurrents, bail expiré et propriétaire obsolète, ordre/vitesse temporels, cache et changement de fichier, protection API locale, segments FAISS, bornes de dates, catalogue dédupliqué, date réelle d'upload playlist, découpage mois/semaine/jour, arrêt budget, protection des médias utilisateur, progression de pagination Archive, recherche Dailymotion vide, masquage de la clé dans les erreurs HTTP.

`reports/analysis-audit.json` et certains anciens rapports sont **historiques** (échecs avant correction, compteurs intermédiaires) ; ne pas les utiliser à la place de `final-audit.json` pour l'état final. Les rapports `baseline-*` sont les premières extractions. Le benchmark final a remplacé `reports/compression.json`.

## Architecture et points à reprendre en priorité

1. **Instrumentation complète des découvertes.** Ajouter un journal d'impressions ou captures normalisées par requête/page, séparer toujours nouveaux/doublons/hors dates, distinguer erreurs d'accès/quotas/limitations du client. Ne pas reconstruire des nombres historiques incertains comme s'ils étaient mesurés.
2. **Cadence et quotas.** `reserve()` utilise `sleep` ; un long `Retry-After` peut rendre une reprise bloquante et peu réactive au fichier STOP. Remplacer par des tâches différées persistantes et un sommeil interruptible. Le plafond local est en UTC, celui de Google peut suivre une autre règle ; ne pas les assimiler.
3. **Étendre le corpus de validation annoté.** Tester encore les faux positifs sur d'autres vidéos SM64, les images statiques/logos, scènes répétées, combinaison miroir + petite incrustation, changements de vitesse non uniformes et montage coupé. Le test contrôlé actuel ne suffit pas à certifier le rappel ou la précision à grande échelle.
4. **Stockage.** Le budget est vérifié avant téléchargement et le plancher libre pendant celui-ci, mais il ne s'agit pas d'une réservation atomique multi-worker couvrant toutes les écritures. `usage()` parcourt l'arborescence ; `cleanup_completed()` parcourt les jobs terminés à chaque lot. À remplacer avant des millions de fichiers. Les téléchargements partiels des échecs restent pour reprise ; leur GC avec TTL manque. Les anciennes générations FAISS ne sont pas automatiquement purgées.
5. **Cache et références.** Les vecteurs sont réutilisés ; les preuves peuvent être retéléchargées si une nouvelle référence trouve un match dont les miniatures ont été nettoyées. La reprise est au niveau **vidéo**, pas exactement à la dernière frame après crash pendant l'extraction. `retry --all-done` permet de rematcher après ajout d'une référence. Une modification en place d'un fichier candidat local déjà mis en cache n'est pas systématiquement revalidée par le chemin rapide du worker ; éviter de modifier ces fichiers silencieusement, ajouter un test et invalider ce cas.
6. **Couverture Archive.** Un fichier choisi par item ; expansion multi-fichiers à implémenter. Distinguer date publique de l'Archive et date originale d'une vidéo réarchivée.
7. **Cascade et compression.** DINO/CLIP est calculé sur toutes les vidéos admissibles. Pas encore de véritable indexation globale légère suivie d'une seconde passe sélective validée. Le pHash ne doit pas éliminer des vidéos. PQ existe dans le benchmark, pas comme stockage de production validé. HNSW shardé est implémenté/testé ; pas de backend distribué.
8. **Paramètres.** CLI et campagne démarrent bien au 15/08/2007, mais les valeurs par défaut des fonctions Python `discovery.discover` / `catalogs.import_catalog` / `catalogs.playlist` restent au 01/01/2007 quand appelées directement sans dates. Uniformiser et ajouter un test. `initialize()` fixe 65 fenêtres jusqu'à 2012 ; périodes hors de ce périmètre restent désactivées, et un mécanisme explicite de campagnes supplémentaires doit être conçu.
9. **Exécution.** Le verrou GPU est pris par la CLI ; les scripts d'audit appellent directement `worker.run`. Ne pas lancer deux de ces scripts à la fois pour un benchmark reproductible. Pas de reprise automatique au démarrage Windows sans relance de la commande.
10. **Installation.** Windows effectivement installé/testé ; script WSL2 et workflow GitHub Actions écrits mais non exécutés. Pas de packaging ou CI distante attestée.

Le README décrit également ces limites. Ne pas présenter cette V1 comme capable de traiter déjà des centaines de millions de vidéos avec 500 Gio : cela n'a pas été démontré et le stockage multi-vues actuel ne le permet pas.

## Commandes utiles de reprise (lecture seulement par défaut)

```powershell
cd .
.\.venv\Scripts\lostmedia status --data data/chronological
.\.venv\Scripts\lostmedia campaign-progress --data data/chronological
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m pip check
git status --short
```

Ne pas lancer `campaign --watch`, `run`, ou les scripts d'audit avant d'avoir relu la demande actuelle de l'utilisateur. Ils effectuent de vrais appels réseau et téléchargements. Le bouton de campagne de l'interface fait la même chose. Le verrou `campaign_through = "2007-08-31"` doit rester en place.

Pour relancer un serveur après avoir vérifié qu'aucun autre n'occupe le port :

```powershell
.\.venv\Scripts\lostmedia serve --data data/chronological --port 8765
# 8766 : lire data_root dans data/validation/report-dinov2.json et l'utiliser comme --data.
```

Pour une future analyse bornée, uniquement si demandée : utiliser les limites spécifiques des scripts d'audit ou passer des Settings explicites ; `lostmedia run` utilise les limites générales plus larges de `config.toml`.

## Fichiers à consulter

- `README.md` : installation, commandes, architecture, limites.
- `config.toml` : paramètres, FP16, budget et verrou temporel.
- `reports/final-audit.json` : compteurs finaux et état de la base.
- `reports/compression.json` : dernier benchmark élargi.
- `data/validation/report-dinov2.json` : validation FP16 et racine du serveur 8766.
- `reports/dailymotion-analysis-expanded.json` : lot 40/40 réel.
- `reports/youtube-analysis-integration.json` : lot YouTube 14/15.
- `reports/youtube-discovery-integration.json` : audit YouTube antérieur à la dernière pagination, donc pas les totaux finaux.
- `reports/youtube-channel-integration.json` : chaîne/playlist réelle.
- `reports/tests.txt` : dernière exécution de tests.
- `data/chronological/catalog.sqlite3` : base persistante de production expérimentale.
- `data/*.log` : sorties des audits ; `data/chronological/` contient aussi les logs des serveurs et les artefacts gérés. Ne pas publier les logs bruts sans revue.
- `data/catalog.sqlite3` : **ancienne base de smoke test**, avec une période/date Archive antérieure à la correction. **Ne pas la confondre avec `data/chronological/catalog.sqlite3`.**

La référence source, les vidéos plantées et les modèles sont déjà présents. Ne pas repartir de zéro et ne pas annoncer les inserts contrôlés comme des correspondances trouvées sur Internet.
