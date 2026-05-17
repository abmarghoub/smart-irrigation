# Plantes adaptées à un essai court (15 à 20 jours maximum)

Ce document liste des cultures ou modèles végétaux sur lesquels on peut mener un **test contrôlé** (irrigation, capteurs, stress hydrique léger, comparaison de traitements) sur une fenêtre de **15 à 20 jours** au maximum. La durée couvre surtout les **phases précoces** (levée, premières feuilles, début de croissance) ou des **micro-cultures** en pot. Le **« jour 0 »** de l’essai peut être le début du **protocole comparatif** avec des **plants déjà achetés ou en pot**, pas forcément le jour du semis (voir section dédiée).

## Critères d’un bon « sujet de test » court

- **Cycle végétatif très court** en phase jeune, ou culture **récoltable jeune** (micro-pousses, jeunes pousses).
- **Peu d’espace** : pots, godets, bacs peu profonds — idéal pour répéter des conditions (avec / sans irrigation pilotée).
- **Réaction visible** en quelques jours si l’eau manque ou excède (flétrissement, jaunissement, croissance ralentie).

---

## Idées de plantes et durées indicatives

| Plante / modèle | Durée d’essai typique | Idée de test |
|-----------------|----------------------|--------------|
| **Laitue (salade)** — variétés précoces | 18–21 j (jeunes plants en pot) | Seuil d’humidité sol, fréquence d’arrosage, comparaison manuel vs décision modèle. |
| **Radis** | 15–25 j (récolte « primeur » souvent ~3 sem.) | Stress hydrique modéré vs irrigation régulière ; croissance racinaire visible. |
| **Roquette / mâche** | 15–20 j (coupe jeune) | Réponse rapide à l’irrigation ; feuillage sensible au manque d’eau. |
| **Épinard** | 15–20 j (feuilles jeunes) | Même logique que roquette ; bon en intérieur ou serre froide. |
| **Cresson / cressonnette** | 7–15 j | Très court ; tests « proof of concept » capteurs / vanne. |
| **Haricot nain** (godet) | 15–20 j (levée → 2–4 feuilles) | Bon modèle pour **stade jeune** et irrigation ciblée ; pas une récolte complète en 20 j. |
| **Orge / blé en pot** (céréale) | 15–20 j | Croissance linéaire visible ; utile pour **benchmark** capteurs (humidité, température). |
| **Basilic / persil** (semis → jeunes plants) | 15–20 j | Sensible au séchage du substrat ; bon pour UI / alertes. |
| **Tomate** (semis → jeune plant) | 15–20 j | Voir encadré ci-dessous : **stade jeune uniquement** (pas de fruits en 20 j). |
| **Micro-pousses** (radis, luzerne, etc.) | 7–12 j | **Hors** fenêtre 15–20 j en durée minimale, mais on peut **enchaîner** 2 cycles en ~20 j pour comparer protocoles. |

### Tomate en essai de 15 à 20 jours

La tomate a un **cycle long** jusqu’à la récolte (souvent **2–3 mois et plus** selon variété et conditions). En **15–20 jours maximum**, l’essai reste donc pertinent sur la **phase précoce** :

- **Semis → levée** (environ quelques jours) puis **premières feuilles vraies** ; plant de **10–25 cm** possible si bonne lumière et chaleur modérée.
- **Intérêt pour ton test** : comportement face à l’**irrigation** (substrat qui sèche entre deux apports), **capteur d’humidité sol**, comparaison **manuel / automatique**, journal CSV / modèle si ta culture « tomate » est dans le dataset — **sans** attendre floraison ni maturation des fruits.
- **Conseils** : variétés **précoces** ou **naines** en pot pour un port plus compact ; éviter le stress hydrique **extrême** (la tomate jeune est sensible mais se remet mal si laissée flétrie trop longtemps).
- Si l’objectif est une **récolte de tomates** dans la même fenêtre temporelle, ce n’est **pas réaliste** : prolonger l’essai ou choisir une autre espèce (radis, salade jeune, etc.).

---

## Ce qu’on peut mesurer en 15–20 jours

- **Humidité du substrat** et corrélation avec l’état des feuilles (turgescence).
- **Température / hygrométrie** ambiante et impact sur la demande en eau.
- **Nombre d’événements d’irrigation** et volume (litres) par rapport à un scénario fixe.
- **Croissance relative** : hauteur, nombre de feuilles, surface foliaire approximative (photos).

---

## Limites à garder en tête

- En **20 jours maximum**, on teste surtout la **réponse précoce** à l’eau, pas une culture commerciale complète (sauf radis, micro-pousses, salades très jeunes). **Tomate** : jeunes plants et irrigation oui ; **fruits** non dans ce délai.
- **Variété et saison** : les durées varient avec la lumière, la température et le volume de pot.
- Pour un **projet type station d’irrigation** (ESP32 / pont PC), privilégier des **pots identiques**, même substrat, et **répliques** (au moins 2 plants par traitement) pour limiter le bruit expérimental.

---

## Commencer sans repartir du semis (« pas à 0 j »)

Tu n’es **pas obligé** de partir d’une graine le jour 0. Beaucoup d’essais comparent plutôt deux **plants déjà formés** (jardinerie, pépinière, ou les tiens déjà en pot).

### Intérêt

- **Homogénéité** : deux tomates du **même bac de vente** ont en général le même âge et la même histoire d’arrosage chez le producteur.
- **Moins d’aléa** : pas d’échec de levée qui ruinerait la comparaison.
- **Test irrigation plus lisible** : la plante transpire déjà un peu plus qu’une toute petite graine ; la demande en eau est plus **stable** dès le début.

### Définir le **J0** de ton essai

- **J0** = le jour où tu appliques **strictement** le protocole comparatif (même pot, même place, A en manuel / B en auto), **pas** le jour du semis.
- Si tu **rempotes** les deux le même jour dans des pots identiques et le même terreau, considère **J0 = jour du rempotage** (ou **J0 = fin d’une courte acclimatation**, voir ci-dessous).

### Acclimatation après achat ou rempotage (recommandé)

- **2 à 4 jours** où les deux plants reçoivent le **même** arrosage manuel soigné (comme la section « Tomate témoin »), **sans** encore laisser le système seul sur B si tu veux une ligne de départ propre.
- Ensuite tu actives le **mode comparaison** : A reste manuel, B passe sous **vanne + logique**.

### Saisie dans le modèle / dashboard (`crop_age_days`, etc.)

- Si tu ne connais pas l’âge exact depuis le semis, mets la **même valeur** pour les deux plants (ex. estimation fournie par le vendeur, ou **âge moyen** d’un plant en godet : souvent **4 à 8 semaines** pour une tomate prête à planter — à convertir en **jours** pour ton champ).
- L’important est que **A et B** aient les **mêmes** paramètres manuels / modèle pour que la comparaison porte sur **l’eau**, pas sur des entrées différentes.

### Attention

- Un plant **trop grand** pour ton petit pot sèche ou étouffe vite : choisir un **volume de pot adapté** à la taille actuelle des deux plants (toujours **identique** entre A et B).

---

## Comparer 2 plants pour juger si le système fonctionne bien

L’idée est de séparer **deux traitements** : un **témoin irrigué manuellement** (référence « ce qu’un humain fait de mieux ») et un **plant piloté par ton système** (capteurs + vanne + logique MLP / règles). La comparaison n’est **juste** que si tout le reste est le plus identique possible.

### Dispositif recommandé

| Élément | Plant **A** (manuel) | Plant **B** (système) |
|--------|----------------------|------------------------|
| **Espèce / variété** | Même chose (ex. 2 tomates jeunes du même lot). | Idem. |
| **Âge / taille au J0** | Plants aussi proches que possible (même semis ou même jardinerie). | Idem. |
| **Pot** | Même volume (ex. 2 × 3 L), mêmes trous de drainage. | Idem. |
| **Substrat** | Même terreau + même quantité. | Idem. |
| **Exposition** | Côte à côte : **même soleil**, même courant d’air (pas l’un au soleil et l’autre à l’ombre). | Idem. |
| **Capteur** | Optionnel sur A pour **noter** l’humidité sans l’utiliser pour arroser ; sur B le capteur pilote le système. | Capteur dans le même **type de position** dans le pot (profondeur, côté). |

### Comment décider si le système « va bien »

- **Vigueur** : hauteur, nombre de feuilles, couleur (vert franc vs jaunâtre), absence de flétrissement prolongé.
- **Substrat** : ni **bouillie permanente** (manque d’air aux racines) ni **sec trop longtemps** (flétrissement le midi).
- **Données** : sur B, consulter ton **journal** (CSV / dashboard) : fréquence des irrigations, volumes, humidité sol si disponible ; sur A, tenir un **carnet simple** (date, heure, « substrat sec en surface oui/non », volume d’eau approximatif en L ou en « verre de X ml »).

### Pièges à éviter

- Comparer une **grosse tomate** en bac avec une **petite** en godet : la demande en eau n’a **rien** à voir.
- Changer la **culture** dans le modèle (ex. « tomate » sur un plant et « autre » sur l’autre) : le modèle ne compare plus la même chose.
- Oublier que le plant manuel peut être **mieux** ou **moins bien** arrosé selon ton habitude : d’où l’intérêt de suivre la section suivante **à la lettre** pour le témoin.

---

## Tomate témoin : irrigation manuelle « correcte » pour une comparaison honnête

Objectif : donner au plant **A** des conditions **proches de l’optimum** pour un **jeune plant en pot** (15–20 jours d’essai), sans sur-arrosage. Tu pourras alors dire : « le système se comporte comme / mieux / moins bien qu’un arrosage manuel soigné ».

### 1. Substrat et pot

- Terreau **universel ou tomates / légumes**, **bien drainé** ; pas de terre lourde seule en fond étanche.
- Pot avec **trous** ; soucoupe : **vider** l’eau stagnante après 30 min–1 h si tu as trop arrosé (évite pourriture des racines).

### 2. Quand arroser (repères simples)

- **Doigt** : enfoncer ~2–3 cm ; si c’est **sec**, arroser. Si encore **humide**, attendre 12–24 h et revérifier.
- **Poids** : soulever le pot ; un pot **léger** = plus sec (utile quand on prend le réflexe).
- **Rythme indicatif** (variable selon chaleur et soleil) : souvent **tous les 1–2 jours** en intérieur chaud ; **parfois chaque jour** si très sec et pot petit. Mieux vaut **souvent un peu** que **rarement à la limite du flétrissement**.

### 3. Comment arroser

- Eau à **température ambiante**.
- Arroser **lentement** sur toute la surface du substrat jusqu’à ce que l’eau **commence à s’échapper** un peu en bas (bonne remontée capillaire), sans laisser le pot **trempé des jours** dans la soucoupe pleine.
- Privilégier le **matin** si possible (la plante affronte la journée avec un substrat homogène).

### 4. Signes que c’est « bien »

- Feuilles **turgescentes** le matin ; pas de flétrissement **récurrent** chaque après-midi (un coup de chaud ponctuel peut faire flétrir un peu : ajuster le rythme).
- Croissance **régulière** ; pas de base de tige noire / molle (signe de trop d’eau prolongé).

### 5. Signes d’alerte

- **Trop sec** : flétrissement qui ne se redresse pas après l’arrosage le soir → augmenter la fréquence ou le volume légèrement.
- **Trop humide** : feuilles jaunes basses, odeur de terreau **surnoî**, limaces ou moisissures en surface → arroser moins souvent, améliorer drainage, vider soucoupe.

### 6. Pour être équitable avec le plant **B** (automatisé)

- Note sur un carnet ou tableau : **date**, **heure**, **observation** (sec 2 cm / humide), **quantité d’eau** (ex. 200 ml).
- Ne « rattrape » pas le plant A par des **gros arrosages** après une longue sécheresse : ça crée un stress que B ne subit pas si ton système est régulier. L’objectif est une **courbe d’eau régulière** des deux côtés, pas des extrêmes sur A seulement.

---

*Document indicatif pour planifier des essais courts ; ajuster les espèces selon climat, espace disponible et objectif (démo technique vs agronomie fine).*
