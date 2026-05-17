# Irrigation intelligente (ESP32) — description du travail et hiérarchie des modèles

Ce document résume les choix faits dans le projet (notebooks `smallNN.ipynb`, `randomforest.ipynb`, `desion tree.ipynb`) et **justifie** quel modèle est considéré comme **principal**, **secondaire** ou **complémentaire**, avec une **comparaison** sur les mêmes données et les mêmes variables d’entrée. Le code de **déploiement sur ESP32** (firmware avec **SmallNN** en principal et **Random Forest** — premier arbre — en secondaire) est décrit au **§6** : variantes **PlatformIO** (`esp32/src/`) et **Arduino IDE** (`esp32/irrigation_esp32/`). La station **capteurs + météo + décision MLP** dans **`esp32_weather_station/`** peut envoyer les données vers un **PC** via **MQTT** (`pc_irrigation_bridge/` : dashboard local, **CSV** et **MySQL**) — voir **§6.10.10** ; les sections **§6.8–6.10** décrivent aussi l’historique (mini météo, journal LittleFS, dashboard embarqué si réactivé).

---

## 1. Ce qui a été mis en place

### 1.1 Données et cibles

- **Fichier** : `Smart_irrigation_dataset.csv`
- **Entrées retenues** (alignées avec `propmt.md`) :
  - **Capteurs** : `soil_moisture_%`, `temperature_C`, `humidity_%`, `rainfall_mm`
  - **Saisie utilisateur** : `crop_name`, `soil_type`, `crop_age_days`
- **Cibles** :
  - **Classification** : `irrigate` (0/1)
  - **Régression** : `irrigation_amount_m3` (volume d’eau, équivalent *water_amount* dans les consignes)

Toutes les autres colonnes du CSV sont **exclues** de l’entraînement pour éviter des fuites d’information et des modèles peu réalistes sur capteurs embarqués.

### 1.2 Prétraitement commun (notebooks `smallNN` et `randomforest`)

- Imputation des valeurs manquantes (médiane pour les numériques, mode pour les catégories).
- **MinMaxScaler** sur les variables numériques.
- **OneHotEncoder** sur `crop_name` et `soil_type`.
- Sauvegarde des pipelines / paramètres (fichiers sous `smallNN/models`, `randomforest/models`, etc.) et export pour embarqué (poids C pour le MLP, `rf_rules.txt`, etc.).

### 1.3 Notebook arbre de décision (`desion tree.ipynb`)

- Même **sélection de colonnes** pour rester cohérent avec le scénario terrain.
- Prétraitement adapté aux arbres : imputation + **MinMax** sur les numériques, **LabelEncoder** pour `crop_name` et `soil_type`.
- Arbres **peu profonds** (`max_depth=4`) pour rester légers et exportables en règles (`dt_rules.txt`).

### 1.4 Complexité des modèles (objectif ESP32 / déploiement léger)

| Modèle | Configuration retenue (ordre de grandeur) |
|--------|----------------------------------------|
| Petit réseau (MLP) | 1 couche cachée, **8 neurones**, ReLU |
| Forêt aléatoire | **tunée via `GridSearchCV`** (classif : 200 arbres, profondeur 10 ; régression : 400 arbres, profondeur 6) |
| Arbre de décision | Profondeur max **4** |

> La forêt est désormais sélectionnée par une grille `n_estimators ∈ {100, 200, 400}`, `max_depth ∈ {6, 10, None}`, `min_samples_leaf ∈ {1, 2}`, validation croisée 3 folds (`accuracy` pour la classif, `neg_mean_squared_error` pour la régression). Cette version est **plus lourde** que la précédente (adaptée à un serveur/gateway) ; pour un déploiement ESP32 strict, revenir à une grille plus contrainte (par ex. `n_estimators ≤ 32`).

---

## 2. Comparaison quantitative (même split, mêmes features)

Les métriques ci-dessous ont été calculées avec **train/test 80/20**, `random_state=42`, **stratification** sur `irrigate` pour la classification, et le **même préprocesseur** (ColumnTransformer + MinMax + OneHot) pour les trois familles afin de les comparer équitablement sur la partie « pipeline sklearn » des notebooks.

### 2.1 Classification — précision sur `irrigate` (jeu de test)

| Modèle | Accuracy |
|--------|----------|
| MLP (8 neurones) | **0,778** |
| **Random Forest tunée** (200 arbres, prof. 10) | **0,772** |
| Arbre de décision (prof. 4) | 0,762 |
| Random Forest bridée ESP32 (16, prof. 4) | 0,685 |

### 2.2 Régression — MSE sur `irrigation_amount_m3` (jeu de test)

| Modèle | MSE (plus bas = mieux) |
|--------|------------------------|
| **Random Forest tunée** (400 arbres, prof. 6) | **≈ 4,32 × 10⁴** |
| MLP (8 neurones) | ≈ 4,72 × 10⁴ |
| Random Forest bridée ESP32 (16, prof. 4) | ≈ 4,80 × 10⁴ |
| Arbre de décision (prof. 4) | ≈ 5,14 × 10⁴ |

> **Note 1** : vos propres exécutions peuvent légèrement varier (initialisation, early stopping du MLP, version de scikit-learn), mais l’ordre reste stable.
>
> **Note 2** : la **Random Forest tunée** utilise des modèles **trop volumineux** pour l’ESP32 (plusieurs centaines d’arbres). Elle sert de **référence** et peut être déployée côté **serveur / gateway**. Pour la cible embarquée, on garde la **version bridée** (16 arbres, prof. 4) — dont le rôle reste la **redondance de modèle** plutôt que le meilleur score brut.

---

## 3. Modèle principal, secondaire et complémentaire

### 3.1 Modèle **principal** : petit réseau de neurones (`smallNN.ipynb`)

**Rôle** : décision principale et estimation du volume sur la cible embarquée (ESP32 / TinyML).

**Justification** :

1. **Performance** : sur ce jeu de données et ce prétraitement, le MLP obtient la **meilleure accuracy** (0,778) ; il est battu en MSE uniquement par la Random Forest **tunée et non-embarquée** (voir tableaux §2). Dans le cadre embarqué strict (RF bridée à 16 arbres/prof. 4), le MLP domine les deux métriques.
2. **Déploiement** : une fois les poids exportés en **C** (`smallnn_*_weights.h`), l’inférence est réduite à des **multiplications matricielles et ReLU**, très adaptées à un microcontrôleur avec peu de RAM si le réseau reste petit (ici une couche de 8 neurones).
3. **Continuité** : le MLP produit des sorties **lisses** (probabilité / valeur réelle), utiles pour combiner seuil d’irrigation et volume sans sauter brutalement d’une feuille d’arbre à l’autre.

### 3.2 Modèle **secondaire** : Random Forest (`randomforest.ipynb`)

**Rôle** : filet de sécurité / modèle de repli lorsque le réseau est **peu fiable** (données hors plage, capteur défaillant, overflow numérique) ou pour **croiser** une décision critique.

**Justification** :

1. **Robustesse conceptuelle** : une forêt agrège plusieurs arbres ; même avec peu d’estimateurs pour l’embarqué, elle offre une **autre structure d’erreur** que le MLP (décisions par seuils sur attributs), ce qui complète le réseau en cas de désaccord.
2. **Pas d’hypothèse de différentiabilité** : contrairement au MLP, les forêts ne reposent pas sur le gradient ni sur des activations continues ; elles peuvent rester **stables** sur des zones où le petit réseau généralise mal.
3. **Compromis embarqué** : avec **16 arbres** et profondeur **4**, on limite la taille pour l’ESP32 ; pour un déploiement moins contraint (gateway, serveur local), le tuning `GridSearchCV` donne une version **fortement améliorée** — cf. §2.
4. **Potentiel démontré** : avec le tuning, la RF atteint **0,772 d’accuracy** (≈ égale au MLP) et **bat le MLP en MSE** (≈ 4,32 × 10⁴). Le rôle secondaire est donc **justifié à la fois par la diversité de modèle et par des performances compétitives** une fois le modèle ajusté.

> La version **bridée pour l’ESP32** reste moins performante que l’arbre seul sur l’accuracy (0,685 vs 0,762) : c’est le prix du faible nombre d’arbres et de la profondeur limitée. La RF retrouve son rôle de secondaire naturel dès qu’on desserre ces contraintes.

### 3.3 Modèle **complémentaire / interprétable** : arbre de décision (`desion tree.ipynb`)

**Rôle** : **explicabilité**, audit, et **dernier recours** très simple (règles `if/else` lisibles dans `dt_rules.txt`).

**Justification** :

1. **Transparence** : un arbre seul se lit comme une **liste de règles** ; utile pour la maintenance, la démonstration pédagogique, ou un mode « dégradé » ultra simple sur la cible.
2. **Coût minimal** : une profondeur 4 donne un nombre limité de nœuds — facile à coder en C sans bibliothèque d’apprentissage.
3. **Position dans la chaîne** : on l’utilise typiquement **après** le MLP (et éventuellement la forêt) lorsqu’on veut **expliquer** une décision ou fournir une **version certifiable** du raisonnement.

### 3.4 Recommandation explicite : **la Random Forest doit être le modèle secondaire**

**Position retenue** : parmi les deux modèles « non neurones », c’est bien la **Random Forest** qui doit jouer le rôle **secondaire** (repli / second avis après le MLP), et **l’arbre de décision** le rôle **complémentaire** (explication et secours minimal), **pas l’inverse**.

**Justification** :

1. **Diversité face au MLP** : le repli utile n’est pas seulement « un autre score sur un benchmark », mais une **famille d’erreurs différente**. Le MLP extrapole de façon **continue** ; la forêt vote sur des **décisions discrètes par seuils** sur des attributs bruts ou déjà normalisés. En cas de capteur bruité ou de valeurs limites, un **vote d’arbres** (même réduit à 16 estimateurs peu profonds) reste une **agrégation** : elle lisse un peu la variance d’un seul partitionnement, ce qu’un **unique** arbre ne fait pas.

2. **Pourquoi pas l’arbre comme secondaire** : placer l’arbre en « n°2 » reviendrait à en faire le **seul** filet par seuils — **une seule partition** du espace, très sensible au jeu d’apprentissage et aux petites variations de mesure. Pour un **second modèle opérationnel** (décision d’arrosage), on préfère une **structure un peu plus stable** que le MLP tout en restant déployable : la forêt bornée (peu d’arbres, faible profondeur) est ce compromis ; l’arbre reste idéal en **n°3** pour **lire** la politique ou tourner un mode ultra dégradé.

3. **Cohérence avec la chaîne d’ingénierie** : le fichier `rf_rules.txt` documente un arbre **représentatif** de la forêt ; le vrai repli logiciel peut implémenter **plusieurs** arbres ou une version simplifiée, alors que `dt_rules.txt` sert surtout à **certifier / expliquer** une logique simple — ce qui correspond bien à une **priorité plus basse** que le secondaire.

4. **Nuance honnête** : la forêt **bridée** (16 arbres, prof. 4) est moins bonne que l’arbre seul en classification pure, mais ce n’est plus le cas dès qu’on la **tune** (cf. §2 — 0,772 vs 0,762 pour l’arbre, et **meilleure MSE que le MLP**). Le choix secondaire est donc **confirmé** : l’arbre reste 3ᵉ et sert surtout d’interprétation / secours minimal.

---

## 4. Synthèse opérationnelle (ordre recommandé sur l’ESP32)

| Priorité | Modèle | Usage typique |
|----------|--------|----------------|
| 1 | **MLP** | Inférence normale ; décision + volume |
| 2 | **Random Forest** | Repli ou vote de confiance si le MLP est incertain ou signal aberrant |
| 3 | **Arbre de décision** | Explication, mode diagnostic, ou secours logiciel minimal |

*Ordre et rôles justifiés en détail au §3.4 (Random Forest = secondaire de principe, même si les scores bruts du §2 peuvent varier selon la contrainte de taille).*

---

## 5. Fichiers utiles générés par les notebooks

| Notebook | Exemples de sorties |
|----------|---------------------|
| `smallNN.ipynb` | `smallnn_classifier_weights.h`, `smallnn_regressor_weights.h`, `scaler_encoder_params.json`, `*.joblib` |
| `randomforest.ipynb` | `rf_rules.txt`, `scaler_encoder_params.json`, `*.joblib` |
| `desion tree.ipynb` | `dt_rules.txt`, `scaler_encoder_params.json`, `*.joblib` |
| Déploiement `esp32/` | Copies embarquées des `.h` SmallNN, `preprocess_params.h`, `rf_tree_clf.h` / `rf_tree_reg.h` (générés par `tools/rf_export_text_to_c.py`), `src/main.cpp`, `platformio.ini` — voir **§6**. |
| `esp32_weather_station/esp32_weather_station.ino` | Croquis **Arduino** ESP32 : capteurs + **Open-Meteo** + **MLP** ; **MQTT** (télémétrie + commandes manuelles) ; par défaut **sans** serveur web ni CSV sur flash — **dashboard, export CSV et base MySQL** sur le PC via **`pc_irrigation_bridge/`** (§**6.10.10**). Les §**6.8–6.10** couvrent l’historique (WiFiManager / météo seule) et le mode **dashboard + LittleFS** si vous réactivez `ENABLE_WEB_DASHBOARD` / `ENABLE_DATASET_LOG` dans `weather_secrets.h`. |

### 5.1 Rôle et explication des fichiers exportés

Les chemins ci-dessous sont relatifs aux dossiers créés par les notebooks (par ex. `smallNN/models/`, `randomforest/models/`, `decision_tree/models/`).

#### `smallnn_classifier_weights.h` (notebook `smallNN.ipynb`)

- **Nature** : en-tête C contenant les **poids et biais** du MLP de **classification** (`irrigate`), sous forme de tableaux `static const float` (matrices aplanies en ordre **row-major**, couche par couche : W0, b0, W1, b1…).
- **Rôle** : permet d’implémenter **l’inférence du modèle principal** sur l’ESP32 **sans** scikit-learn ni Python — seulement des multiplications, additions, ReLU et (selon la dernière couche) une sigmoïde ou un seuil sur la sortie.
- **Usage typique** : inclure ce fichier dans le firmware (`#include "smallnn_classifier_weights.h"`) et recoder une petite fonction `forward()` qui reproduit la même architecture que dans le notebook (entrée = vecteur déjà prétraité, voir ci-dessous).

#### `smallnn_regressor_weights.h` (notebook `smallNN.ipynb`)

- **Nature** : même principe que le fichier précédent, mais pour le MLP de **régression** (volume d’eau / `irrigation_amount_m3`).
- **Rôle** : estimer **combien d’eau** appliquer lorsque l’irrigation est pertinente, toujours en **C pur** sur la cible.
- **Usage typique** : chaînage logique sur l’ESP32 : d’abord classification (irriguer oui/non), puis si oui, appel du régresseur pour le volume (ou les deux réseaux peuvent être utilisés selon votre logique métier).

#### `scaler_encoder_params.json` (notebooks `smallNN`, `randomforest`, `desion tree`)

- **Nature** : fichier **JSON** lisible par un humain ou un script ; contient les paramètres du **MinMaxScaler** sur les colonnes numériques (`data_min`, `data_max`, `scale`, `min`, noms des colonnes) et, pour les notebooks avec **OneHotEncoder**, les **catégories** par variable catégorielle (`onehot_categories`, `onehot_feature_names_in`).
- **Rôle** : **reproduire exactement le prétraitement Python** sur l’ESP32 (normalisation [0,1], puis encodage one-hot ou équivalent) pour que les entrées du MLP ou des règles RF correspondent à ce que le modèle a vu à l’entraînement.
- **Usage typique** : soit recoder les formules MinMax + one-hot en C à partir des nombres du JSON, soit générer un second petit fichier C (`scaler_params.h`) à la main ou par script à partir de ce JSON.
- **Note** : le notebook **arbre de décision** exporte un JSON de structure proche mais basé sur **LabelEncoder** (listes `crop_classes`, `soil_classes`) plutôt que one-hot — lire le fichier produit pour ce notebook précisément.

#### Fichiers `*.joblib` (tous les notebooks concernés)

- **Nature** : **sérialisation binaire** Python (joblib) d’objets scikit-learn : pipelines complets (`smallnn_classifier.joblib`, `randomforest_classifier.joblib`, …), préprocesseurs seuls (`smallnn_preprocessor.joblib`, …), ou modèles + bundle de préparation (`decision_tree_preprocessing.joblib`).
- **Rôle** :
  - **Sur PC** : recharger un modèle **sans ré-entraîner** pour tests, courbes, comparaisons ou service Python.
  - **Sur ESP32** : en général **on ne charge pas** les `.joblib` sur le microcontrôleur ; ils servent de **référence** et de source pour générer les exports « embarqués » (`.h`, `.json`, règles texte).
- **Usage typique** : `model = joblib.load("smallnn_classifier.joblib")` puis `model.predict(X)` dans un script de validation ou une API locale.

#### `rf_rules.txt` (notebook `randomforest.ipynb`)

- **Nature** : fichier texte contenant une représentation **lisible** du premier arbre de chaque forêt (classification et régression), au format **`export_text`** de scikit-learn (seuils du type `num__temperature_C <= 0.19`, feuilles = classe ou valeur prédite).
- **Rôle** : documenter ou **porter à la main** la logique « arbre » de la forêt **secondaire** : aide à écrire des `if/else` en C, à auditer le modèle, ou à comparer visuellement avec le MLP.
- **Limitation** : une forêt complète = plusieurs arbres ; ce fichier ne montre **qu’un arbre représentatif par tâche** (volontairement léger pour l’embarqué). Ce n’est pas équivalent mathématique à toute la forêt, mais c’est un **extrait interprétable** utile pour le repli RF.

#### `dt_rules.txt` (notebook `desion tree.ipynb`, pour cohérence avec le tableau du §5)

- **Nature** : règles d’**arbre de décision** complet (classifieur + régresseur) au format `export_text`, plus lisible qu’une forêt entière.
- **Rôle** : même idée que `rf_rules.txt` mais pour le modèle **interprétable / complémentaire** ; idéal pour expliquer une décision ou un mode dégradé en `if/else` sur l’ESP32.

---

## 6. Déploiement ESP32 (`esp32/`)

Le dossier **`esp32/`** contient un firmware **Arduino / PlatformIO** qui applique la hiérarchie **SmallNN = modèle principal**, **Random Forest = modèle secondaire**, conformément aux §3 et §4.

### 6.1 Contenu et rôle des fichiers

| Élément | Rôle |
|---------|------|
| `src/main.cpp` | Prétraitement (MinMax + one-hot), inférence **MLP** (classification + régression volume), parcours du **premier arbre RF** en secours, politique de fusion (voir §6.3). |
| `include/smallnn_classifier_weights.h`, `include/smallnn_regressor_weights.h` | **Copies** des exports `smallNN/models/*.h` — poids du réseau principal. |
| `include/preprocess_params.h` | Paramètres **alignés** sur `smallNN/models/scaler_encoder_params.json` (échelle et `min` sklearn pour les 5 numériques ; one-hot 4×4 pour cultures et sols). |
| `include/rf_tree_clf.h`, `include/rf_tree_reg.h` | **Premier arbre** de chaque forêt RF (classification `irrigate`, régression `irrigation_amount_m3`), converti en tables de nœuds C (`RfClfNode` / `RfRegNode`) à partir de `randomforest/models/rf_rules.txt`. Ce n’est **pas** toute la forêt : c’est l’approximation **documentée** dans le projet (un arbre représentatif par tâche), exploitable en repli sur microcontrôleur. |
| `platformio.ini` | Cible `esp32dev`, framework Arduino, moniteur série 115200 baud. |
| `irrigation_esp32/irrigation_esp32.ino` + tous les `.h` dans le même dossier | **Croquis Arduino IDE** (même firmware que `src/main.cpp`). Ouvrir `irrigation_esp32.ino` dans l’IDE ; pas de sous-dossier `include/`. |
| `tools/rf_export_text_to_c.py` (à la racine du projet) | Script **à relancer** après régénération de `rf_rules.txt` : parse le format `export_text` sklearn et régénère `esp32/include/rf_tree_clf.h` et `rf_tree_reg.h`. |

Après régénération des en-têtes RF ou mise à jour des poids SmallNN, **recopier** les fichiers `esp32/include/*.h` vers `esp32/irrigation_esp32/` (même contenu) pour garder le croquis Arduino à jour.

### 6.2 Régénération des en-têtes RF

Après avoir ré-entraîné la forêt et exporté à nouveau `randomforest/models/rf_rules.txt` depuis `randomforest.ipynb`, exécuter à la racine du projet :

```text
python tools/rf_export_text_to_c.py
```

Les fichiers `esp32/include/rf_tree_clf.h` et `esp32/include/rf_tree_reg.h` sont alors mis à jour (nombre de nœuds et seuils). Pensez à **recopier** ces `.h` (et les autres depuis `esp32/include/`) dans `esp32/irrigation_esp32/` si vous utilisez Arduino IDE.

### 6.3 Politique embarquée (principal vs secondaire)

1. **Prétraitement** : construction d’un vecteur **13 dimensions** identique au pipeline Python (5 numériques normalisées, puis one-hot cultures puis sols).
2. **Classification** : le MLP sort un **logit** ; la probabilité est `sigmoid(logit)`. Si `|P − 0,5| ≥ 0,15` (marge de confiance configurable dans `main.cpp` ou `irrigation_esp32.ino`), la décision **irriguer oui/non** et le **volume** (si oui) viennent **uniquement du SmallNN** (classifieur + régresseur MLP).
3. **Repli RF** : si la probabilité MLP est **trop proche de 0,5** (zone d’incertitude), le firmware utilise le **premier arbre** de la RF pour la **classification**, puis le **premier arbre** de la RF **régression** pour le **volume** d’eau lorsque l’irrigation est requise. Ainsi le secondaire RF prend le relais **lorsque le principal est peu tranché**, tout en restant cohérent avec la logique « second avis » du §3.2.

### 6.4 Compilation (PlatformIO)

Avec [PlatformIO](https://platformio.org/) installé, dans le dossier `esp32/` :

```text
pio run
pio run -t upload
```

Puis moniteur série (115200 baud) selon la carte réelle (USB, port COM, etc.).

### 6.5 Arduino IDE — installer le support ESP32 et téléverser

1. **Installer le support des cartes Espressif**  
   - Ouvrir **Arduino IDE** (1.8.x ou 2.x).  
   - **Fichier → Préférences** (ou **Paramètres**).  
   - Dans **URL de cartes supplémentaires**, ajouter une ligne (si elle n’y est pas déjà) :  
     `https://espressif.github.io/arduino-esp32/package_esp32_index.json`  
   - Valider.

2. **Installer le gestionnaire de cartes ESP32**  
   - **Outils → Type de carte → Gestionnaire de cartes…**  
   - Rechercher **esp32** (par **Espressif Systems**).  
   - Installer la dernière version stable (cela télécharge le compilateur et les bibliothèques pour ESP32).

3. **Choisir la carte et le port**  
   - **Outils → Type de carte** : par ex. **ESP32 Dev Module** (équivalent à la cible `esp32dev` du PlatformIO).  
   - Brancher la carte en USB.  
   - **Outils → Port** : sélectionner le **COM** correspondant (Windows : ex. `COM3` ; si rien n’apparaît, installer le pilote **CP210x** ou **CH340** selon la puce USB de la carte).

4. **Ouvrir le croquis**  
   - **Fichier → Ouvrir** et ouvrir  
     `esp32/irrigation_esp32/irrigation_esp32.ino`  
   - Vérifier que les fichiers **`.h`** sont bien **dans le même dossier** que `irrigation_esp32.ino` (pas dans un sous-dossier `include`).

5. **Compiler et téléverser**  
   - **Croquis → Vérifier / Compiler**.  
   - **Croquis → Téléverser**.  
   - Si l’upload échoue : appuyer sur **BOOT** sur la carte au moment du téléversement, ou réduire la vitesse d’upload dans **Outils**.

6. **Moniteur série**  
   - **Outils → Moniteur série** (ou icône loupe), **115200 baud** — le programme affiche les probabilités MLP et la décision d’irrigation.

### 6.6 Capteurs physiques (connexion à l’ESP32)

Configuration **matériel réel** prise en charge dans le code : **DHT22**, sonde **humidité sol B42** (analogique), **débitmètre YF-S201**, **électrovanne 12 V** (via relais). Détail dans `irrigation_sensors.cpp` et `irrigation_esp32.ino`.

| Variable modèle | Matériel | Valeur utilisée |
|-----------------|----------|-----------------|
| `soil_moisture_%` | **Sonde humidité sol B42** (analogique) | Lecture **ADC GPIO34** → % (calibration `set_soil_calibration`). |
| `temperature_C` | **DHT22 B42** | Lecture capteur ; si échec → **0**. |
| `humidity_%` | **DHT22 B42** | Lecture capteur ; si échec → **0**. |
| `rainfall_mm` | *Pas de pluviomètre* | **Toujours 0** (pas de capteur). |
| `crop_age_days` | **Saisie utilisateur** (port série) | Commande `A <jours>` (1–120), valeur par défaut 45 au démarrage. |
| `crop_name`, `soil_type` | **Saisie utilisateur** (indices 0…3) | Commandes `C <0-3>` (Maize, Rice, Tomato, Wheat) et `S <0-3>` (Clayey, Loamy, Sandy, Silty). `H` = aide. |
| Débit (hors modèle) | **YF-S201 B89** | **GPIO5** ; affichage L/min uniquement (`irrigation_sensors_get_flow_lpm()`), **pas** une entrée du réseau. |
| `crop_name`, `soil_type` | Indices fixes dans le code | `CROP_IDX` / `SOIL_IDX` (0…3), pas des capteurs. |
| Commande arrosage | **Électrovanne 12 V B90** | **GPIO18** → module **relais / MOSFET** (jamais la vanne en direct sur l’ESP32). |

**Librairies Arduino IDE** : **DHT sensor library** (Adafruit) et **Adafruit Unified Sensor**.

**Fichiers** : `esp32/irrigation_esp32/irrigation_sensors.cpp`, `irrigation_sensors.h`, `irrigation_types.h` ; miroir `esp32/src/` et `esp32/include/`.

### 6.7 Météo de la zone (optionnel — non utilisé par défaut dans le croquis d’irrigation)

Le croquis principal `irrigation_esp32.ino` utilise **uniquement les capteurs listés au §6.6** : pas d’appel réseau par défaut. Des fichiers **`weather_meteo.*`** / **`weather_secrets.h`** peuvent exister pour une variante **OpenWeatherMap** (WiFi + lat/lon saisis) si vous réintégrez vous-même **`weather_meteo_begin()`** et **`weather_meteo_apply_to_raw()`** après `irrigation_sensors_read_raw()` ; dans ce cas, installer **ArduinoJson** v6 et activer **`OWM_ENABLED`** dans `weather_secrets.h`.

Pour une intégration **sans clé API**, avec **configuration WiFi depuis le téléphone** et **coordonnées automatiques** (géoloc par IP), utiliser plutôt le croquis décrit au **§6.8** (`esp32_weather_station/`), puis fusionner la logique métier (seuils pluie / vent) dans votre boucle d’irrigation.

---

### 6.8 Mini station météo ESP32 (`esp32_weather_station/`)

Ce bloc décrit le croquis **`esp32_weather_station/esp32_weather_station.ino`**. **Par défaut** (`ENABLE_LOCAL_SENSORS` **0**) : **aucun** capteur pluie / vent au sol — la pluie et le vent viennent **uniquement d’Open-Meteo** (`readRainApi()`, `readWindSpeedApi()`, `fetchOpenMeteo()`), avec **WiFi** configuré depuis le **téléphone** et **lat/lon** issus de la **géoloc par IP**. Utile pour alimenter `rainfall_mm` / règles de vent **sans pluviomètre ni anémomètre**. Avec **`ENABLE_LOCAL_SENSORS` 1**, les broches locales sont utilisées **en plus** de l’API.

#### 6.8.1 Rôle du programme

| Élément | Description |
|---------|-------------|
| **Exécution** | Le code tourne sur l’**ESP32** après téléversement depuis l’**Arduino IDE** (ou équivalent). |
| **Mode par défaut** | **`ENABLE_LOCAL_SENSORS 0`** : pas de `pinMode` / interruption sur les GPIO pluie-vent ; `readRain()` et `readWindSpeed()` renvoient **toujours 0** — seules les valeurs **API** sont pertinentes pour l’irrigation. |
| **Capteurs locaux (optionnel)** | Si **`ENABLE_LOCAL_SENSORS 1`** : pluie **DO/AO** (`RAIN_INPUT_MODE`), vent **impulsions** + IRQ (`readWindSpeed()` + `WIND_MS_PER_HZ`). |
| **Fonctions** | **API** : `fetchOpenMeteo()`, `readRainApi()` (0/1), `readWindSpeedApi()` (m/s). **Local** (si capteurs) : `readRain()`, `readWindSpeed()`. |
| **WiFi (téléphone)** | **WiFiManager** : AP du type **`ESP32-Meteo`** (`WIFI_CONFIG_AP_NAME`), configuration SSID/mot de passe via le navigateur (souvent `http://192.168.4.1`), identifiants en **flash**. |
| **Latitude / longitude automatiques** | **ip-api.com** (HTTP) après connexion ; **Preferences** (`weather` : `lat`, `lon`, `geo_ok`). Rafraîchissement géo : au **démarrage** puis **toutes les 24 h** ; si zone inconnue, retry **5 min**. |
| **Open-Meteo** | **HTTPS** `api.open-meteo.com` ; `precipitation` / `rain` ; `windspeed_10m` en **m/s** (`windspeed_unit=ms`). |
| **Affichage série** | Mode API seul : ligne du type **`API rain=… wind=… m/s precip … mm [lat … lon …]`**. Mode local + API : préfixe **Local …** puis **API …**. |
| **Drapeaux dans le `.ino`** | **`ENABLE_OPEN_METEO 0`** : pas de WiFi / Open-Meteo (uniquement utile si **`ENABLE_LOCAL_SENSORS 1`**). **`ENABLE_LOCAL_SENSORS 1`** : active GPIO + lectures locales **en plus** de l’API si celle-ci est activée. |

#### 6.8.2 Câblage

- **Sans capteurs** (`ENABLE_LOCAL_SENSORS` **0**, défaut) : **aucun câblage** pluie/vent requis pour ce croquis.  
- **Avec capteurs** (`ENABLE_LOCAL_SENSORS` **1**) : **pluie** (ex. FC-37) DO → `RAIN_DIGITAL_PIN` (ex. GPIO25), AO optionnel ADC1 (ex. GPIO33) ; **anémomètre** → `WIND_PULSE_PIN` (ex. GPIO18) + pull-up — **calibrer** `WIND_MS_PER_HZ` ; **éviter les conflits** avec l’irrigation (§6.6, ex. ne pas utiliser GPIO18 si réservé à la vanne).

#### 6.8.3 Bibliothèques Arduino à installer

1. **ArduinoJson** (Benoit Blanchon), **v6.x** — pour parser les JSON Open-Meteo et ip-api.  
2. **WiFiManager** — version **compatible ESP32** (dépôt *tzapu* ou fork maintenu type *tablatronix* selon votre environnement).

#### 6.8.4 Intégration avec l’irrigation « intelligente »

- **Sans capteurs au sol** : utiliser **`readRainApi()`**, **`readWindSpeedApi()`** et éventuellement la valeur **mm** précipitation (champs Open-Meteo déjà agrégés dans le croquis) pour des **règles** (ne pas arroser si pluie récente modèle, réduire si vent fort) ou pour **mapper** une entrée type `rainfall_mm` (attention : l’échelle doit rester **cohérente** avec l’entraînement — `scaler_encoder_params.json` / `preprocess_params.h`).  
- **Avec capteurs** (`ENABLE_LOCAL_SENSORS` 1) : possibilité de **combiner** `readRain()` / `readWindSpeed()` avec l’API (vote, seuils, priorité locale).  
- **Broches** : en mode **API seul**, les GPIO pluie/vent du croquis météo **ne sont pas utilisés** — pas de conflit matériel avec l’irrigation pour ces broches **tant que** ce croquis reste séparé ; en fusionnant le code dans `irrigation_esp32`, ne réserver des GPIO locales **que si** `ENABLE_LOCAL_SENSORS` est activé.

#### 6.8.5 Limites à connaître

- La **géoloc IP** reflète la zone de l’**IP publique** (box, 4G partagée, VPN), pas la position GPS du téléphone au moment de la configuration.  
- **ip-api.com** : usage **non commercial** raisonnable ; service en **HTTP** (pas HTTPS) sur la couche gratuite — à utiliser sur un réseau de confiance.  
- **Open-Meteo** : données de **modèle** pour le point `lat`/`lon`, pas une mesure au sol chez vous.

---

### 6.8.6 Étapes à faire **vous-même** (checklist)

**Cas le plus courant — météo uniquement par API** (`ENABLE_LOCAL_SENSORS` laissé à **0**, défaut du croquis) :

1. Installer **Arduino IDE** + support carte **ESP32** (§6.5).  
2. Installer **ArduinoJson v6** et **WiFiManager** (ESP32).  
3. Ouvrir `esp32_weather_station/esp32_weather_station.ino`.  
4. Laisser **`ENABLE_OPEN_METEO 1`** et **`ENABLE_LOCAL_SENSORS 0`** (pas de câblage pluie/vent nécessaire).  
5. Ajuster si besoin : `API_RAIN_DETECT_MM`, intervalles `OPEN_METEO_FETCH_MS` / `GEO_IP_*`.  
6. Téléverser (USB, pilotes, BOOT si besoin — §6.5).  
7. Moniteur série **115200 baud**.  
8. Configurer le **WiFi** depuis le téléphone (AP **`ESP32-Meteo`** → page de configuration).  
9. Vérifier les lignes **`API rain=… wind=…`** et **`precip` / `[lat lon]`**.  
10. Fusion éventuelle avec `irrigation_esp32` : réutiliser **`readRainApi()`**, **`readWindSpeedApi()`**, **`fetchOpenMeteo()`**, WiFi / géoloc — **sans** GPIO locaux si vous restez en API seul.

**Si vous ajoutez des capteurs pluie / vent plus tard** :

11. Mettre **`ENABLE_LOCAL_SENSORS 1`**, câbler selon §6.8.2, régler broches / `RAIN_INPUT_MODE` / `WIND_MS_PER_HZ`, vérifier l’absence de conflit avec §6.6.  
12. Le moniteur série affiche alors **Local …** et **API …**.

**Autres** :

13. **`ENABLE_OPEN_METEO 0`** : uniquement des capteurs locaux (nécessite **`ENABLE_LOCAL_SENSORS 1`** pour un comportement utile).  
14. Changer de box / lieu : redémarrage → nouvelle géo IP ; effacer le WiFi WiFiManager : reset firmware / effacement NVS selon la carte.

---

### 6.9 Croquis recréé (capteurs + station météo + décision)

Le fichier `esp32_weather_station/esp32_weather_station.ino` a été recréé pour votre cas d’usage :

- lecture **capteurs physiques** : `DHT22` (GPIO4), humidité sol B42 analogique (GPIO34), débit `YF-S201` (GPIO5), commande vanne 12V via relais (GPIO18) ;
- lecture **station météo zone** via Open-Meteo (géolocalisation IP + WiFi) pour `temperature_C`, `humidity_%`, `rainfall_mm` si disponible ;
- saisie manuelle utilisateur pour `crop_age_days`, `crop_name`, `soil_type` via port série (`A`, `C`, `S`) ;
- inférence principale **SmallNN** (classification + volume) puis décision `irriguer ou non`, avec repli simple si probabilité MLP incertaine.

Le dossier contient aussi `preprocess_params.h`, `smallnn_classifier_weights.h`, `smallnn_regressor_weights.h` et `weather_secrets.h` (SSID/mot de passe WiFi à renseigner).

---

### 6.10 Journal des évolutions — croquis `esp32_weather_station/` (état consolidé)

Cette section **enregistre** les travaux réalisés sur le firmware **Arduino** `esp32_weather_station/esp32_weather_station.ino` et les fichiers associés, au-delà de la description initiale des §6.8–6.9. Elle sert de **référence unique** pour reproduction, maintenance et dépannage.

#### 6.10.1 Capteurs et matériel

- **DHT22** sur **GPIO4** (remplacement d’un DHT11 éventuel) ; bibliothèque `DHT` en mode `DHT22`.
- **Sonde sol B42** : ADC **GPIO34** ; constantes de calibration `SOIL_ADC_DRY` / `SOIL_ADC_WET` dans le `.ino`.
- **Débitmètre YF-S201** : **GPIO5** avec interruption ; `FLOW_PULSES_PER_LPM` (typ. 7,5).
- **Vanne** : **GPIO18** (via relais / MOSFET).

#### 6.10.2 Géolocalisation et station météo (réseau)

- **Problème rencontré** : `ip-api.com` pouvait refuser la connexion (*connection refused*).
- **Mesures prises** :
  - **Fallback HTTPS** vers **`ipwho.is`** si `ip-api` échoue.
  - Option **`WEATHER_HAS_FIXED_GEO`** / **`WEATHER_FIXED_LAT`**, **`WEATHER_FIXED_LON`** dans `weather_secrets.h` pour forcer des coordonnées si les services IP sont bloqués.
- **Open-Meteo** : requêtes **HTTPS** via **`WiFiClientSecure`** ; logs série **`[GEO]`**, **`[METEO]`** pour diagnostic.

#### 6.10.3 Fusion des données (entrées modèle / sécurité)

- **Température** utilisée pour le modèle : **toujours le capteur local** (DHT) ; pas de remplacement par la température station.
- **Humidité air** : si lecture DHT invalide, **complément** possible depuis la station.
- **Pluie** : pas de pluviomètre local → **pluie modèle** (station) intégrée dans `raw[3]` lorsque disponible.
- **Vent** : vitesse depuis l’API ; **seuil de blocage d’arrosage** (`WIND_BLOCK_THRESHOLD_MS`, ex. 8 m/s) pour la sécurité.

#### 6.10.4 Régression MLP, doses et cohérence « litres »

- Constat : dans le CSV d’entraînement, **`irrigation_amount_m3`** correspond en pratique à des **litres** (nom trompeur).
- **Réentraînement** : cible de régression transformée (ex. **`sqrt`** + **clip** sur les litres), script **`tools/retrain_smallnn_plant_scale.py`**, notebook **`smallNN.ipynb`** (split stable, `clone` du préprocesseur, export des `.h`).
- **Firmware** : plafond **`MLP_PRED_MAX_LITERS`**, échelle **`WATER_REQUEST_SCALE`**, doses **`MIN_DOSE_LITERS`** / **`MAX_DOSE_LITERS`**, **`IRRIGATION_COOLDOWN_MS`**, objectif sol par type **`SOIL_TARGET_PCT_BY_TYPE`**, hystérésis **`SOIL_HYSTERESIS_PCT`**.
- **Affichage série** : suppression de l’affichage explicite de **`dose_l_demande`** au profit des informations dose livrée / cible et décision.

#### 6.10.5 Tableau de bord web embarqué *(désactivé par défaut — voir §6.10.10)*

> **Architecture actuelle** : `ENABLE_WEB_DASHBOARD` vaut **0** par défaut dans `weather_secrets.h` ; le tableau de bord est servi sur le **PC** par Flask (`pc_irrigation_bridge/dashboard.html`). Le texte ci-dessous s’applique si vous remettez le dashboard sur l’ESP (`ENABLE_WEB_DASHBOARD 1`).

- **Fichier** : `esp32_weather_station/web_dashboard.h` — page HTML/CSS/JS en chaîne **`DASHBOARD_INDEX_HTML`**.
- **Activation** : `ENABLE_WEB_DASHBOARD`, port **`DASHBOARD_PORT`** dans `weather_secrets.h`.
- **Serveur** : **`WebServer`** ; routes **`GET /`**, **`GET /api/state`** (JSON capteurs, météo, entrées modèle, décision, manuel), **`POST /api/manual`** (formulaire `application/x-www-form-urlencoded`).
- **Boucle** : `handleClient()` ; cadence métier **~5 s** lorsque le dashboard est activé (pour ménager l’ESP32).
- **Corrections UI/JS** : garde **`Number.isFinite`**, fonction **`fmt()`** pour éviter les plantages **`undefined.toFixed()`** ; **`fetch`** avec **`cache: "no-store"`** ; affichage d’erreurs **`#poll_err`** ; barres (sol, T, RH, météo, pluie, vent, P, volume) et cartes statistiques.
- **JSON** : document **`StaticJsonDocument`** (taille augmentée, ex. 1536), **`Content-Type`** JSON ; champ **`decision.prediction_active`** aligné sur la saisie manuelle confirmée.

#### 6.10.6 Saisie manuelle et prédiction MLP conditionnelle

- **Au démarrage** : **`g_manual_ready = false`** — **aucun** appel à **`decide()`** / MLP tant que l’utilisateur n’a pas confirmé la saisie.
- **Capteurs et météo** : toujours lus et affichés (série + API) ; **vanne / dose** pilotées uniquement après logique de décision active (donc pas d’irrigation « modèle » sans confirmation).
- **Confirmation** : bouton **Appliquer** sur le dashboard (**`POST /api/manual`**) ou commandes série **`A`** (âge), **`C`** (culture 0–3), **`S`** (sol 0–3), **`H`** (aide).
- **Série** : après chaque commande valide, récap **`[MANUEL] (serie) saisie confirmée ->`** ; après le web, **`[MANUEL] (dashboard) saisie confirmee ->`**.
- **Dashboard** : formulaire **sans valeurs par défaut** (listes « Choisir », âge requis) ; carte **Connexion** avec **débit**, **saisie** (active ou en attente), **décision** ; synchronisation des champs formulaire ↔ ESP **uniquement** lorsque **`manual.confirmed`** est vrai.
- **API** : tant que non confirmé, champs manuels / âge modèle en **`null`** ou vides dans le JSON pour éviter d’afficher un faux état initial.

#### 6.10.7 Journal de données CSV (LittleFS) et export *(désactivé par défaut — voir §6.10.10)*

> **Architecture actuelle** : `ENABLE_DATASET_LOG` vaut **0** par défaut ; le journal **`irrigation_log.csv`** est écrit sur le **PC** par `pc_irrigation_bridge/bridge.py` (même logique métier que les mesures, sans saturer la flash). Le texte ci-dessous décrit l’ancien journal **sur l’ESP** si vous réactivez `ENABLE_DATASET_LOG` **et** le dashboard pour la route d’export.

- **Fichiers** : `esp32_weather_station/dataset_log.h`, **`dataset_log.cpp`**.
- **Stockage** : **`LittleFS`** ; fichier **`/irrigation_log.csv`** ; colonnes en **français** : horodatage, **mesures locales** (température, humidité air, pluie modèle, vent, humidité sol, débit), **météo** (température, humidité, pluie, vent), puis **sorties** (`prediction_mlp_activee`, `probabilite_irrigation`, `demande_irrigation`, `volume_modele_litres`, `cible_humidite_sol_pct`, vanne et doses, `vent_bloque_arrosage`). Les champs saisie manuelle (culture, sol, âge) et la colonne **`repli_regles_simples`** ne sont pas journalisés. Filtre téléchargement : premier champ **`horodatage_unix`** (anciens fichiers : `ts_unix`).
- **Horloge** : **NTP UTC** (`configTime`, fuseau **`UTC0`**), serveur configurable **`DATASET_NTP_SERVER`** dans `weather_secrets.h` ; tant que l’heure n’est pas valide, **`horodatage_unix = 0`** et **`date_heure_utc = NO_NTP`**.
- **Écriture** : **`DATASET_LOG_INTERVAL_MS`** (défaut **30 s**) ; rotation si taille **`DATASET_LOG_MAX_BYTES`** (défaut **280 Ko**) — fichier tronqué et réinitialisé avec en-tête.
- **Téléchargement** : **`GET /api/dataset.csv`** ; paramètres optionnels **`start`** et **`end`** (timestamp **Unix UTC** en secondes) pour filtrer les lignes ; sans paramètres, envoi du fichier entier si taille modérée (sinon filtrage par lecture ligne à ligne).
- **Interface** : section **Exporter le dataset** dans `web_dashboard.h` (**`datetime-local`** + bouton **Télécharger dataset** qui construit l’URL avec `start` / `end`).
- **Partition Arduino** : schéma avec **espace data** (LittleFS / SPIFFS selon le core) ; si montage échoue ou erreur **LittleFS corrompu** (*Corrupted dir pair*), utiliser **Erase All Flash Before Sketch Upload : Enabled** pour **un** téléversement puis repasser sur **Disabled** pour ne pas effacer le journal à chaque upload.

#### 6.10.8 Téléversement et dépannage matériel

- Erreur **`Wrong boot mode (0x13)`** : mettre l’ESP32 en **mode bootloader** (maintenir **BOOT**, appuyer sur **RESET**, relâcher selon la carte) pendant l’upload ; vérifier câble USB données, port COM, vitesse d’upload.
- **Port COM occupé** ou échec d’upload : fermer le **moniteur série**, tout autre logiciel utilisant le port, puis réessayer ; vérifier le **bon port** dans le menu Outils Arduino.

#### 6.10.9 Fichiers du dossier `esp32_weather_station/` (référence rapide)

| Fichier | Rôle |
|---------|------|
| `esp32_weather_station.ino` | Boucle principale : capteurs, météo, MLP, dose, vanne, série ; **MQTT** (`mqtt_irrigation_*`) ; **`WebServer`** / **`dataset_log_try_append`** uniquement si activés dans `weather_secrets.h`. |
| `weather_secrets.h` | WiFi, géo fixe, flags **`ENABLE_WEB_DASHBOARD`**, **`ENABLE_DATASET_LOG`**, **`ENABLE_MQTT`**, paramètres broker / topics MQTT. |
| `mqtt_irrigation.h` / `mqtt_irrigation.cpp` | PubSubClient : publish télémétrie JSON, subscribe commandes manuelles (voir **§6.10.10**). |
| `web_dashboard.h` | Page web embarquée (si `ENABLE_WEB_DASHBOARD 1`) : saisie, connexion, barres, cartes, export CSV. |
| `dataset_log.h` / `dataset_log.cpp` | LittleFS, NTP, append périodique, route **`/api/dataset.csv`** (si `ENABLE_DATASET_LOG 1`). |
| `preprocess_params.h`, `smallnn_*_weights.h` | Prétraitement et poids MLP embarqués (mis à jour après réentraînement). |

#### 6.10.10 Architecture « pont PC » : MQTT, dashboard local, CSV et MySQL

Cette section décrit l’**évolution majeure** du déploiement : alléger la charge **RAM / flash** de l’ESP32 en déplaçant **tableau de bord**, **journal CSV** et **base de données** sur un **PC** du même réseau local, tout en conservant sur la carte la **lecture des capteurs**, la **décision MLP** et la **commande vanne**.

##### Objectifs

- L’**ESP32** ne fait plus office de serveur HTTP ni d’enregistrement LittleFS pour le dataset (par défaut).
- Un programme **Python** sur le PC (**`pc_irrigation_bridge/`**) : souscrit au **MQTT**, met à jour un **dashboard web** (Flask), append un **CSV** sur disque, et insère les lignes dans **MySQL** (optionnel).
- **Même chaîne de décision** qu’avant sur l’ESP (SmallNN, météo Open-Meteo, doses, sécurité vent).

##### Côté ESP32 (`esp32_weather_station/`)

| Élément | Détail |
|---------|--------|
| **Fichiers ajoutés** | `mqtt_irrigation.h`, **`mqtt_irrigation.cpp`** — client **PubSubClient** (bibliothèque Arduino à installer). |
| **`weather_secrets.h`** | Par défaut : **`ENABLE_WEB_DASHBOARD 0`**, **`ENABLE_DATASET_LOG 0`**, **`ENABLE_MQTT 1`** ; paramètres **`MQTT_BROKER_HOST`** (IP du PC ou du broker sur le LAN, **pas** `127.0.0.1` vu depuis l’ESP — trouver l’IPv4 du PC avec **`ipconfig`** sous Windows), **`MQTT_BROKER_PORT`**, **`MQTT_TOPIC_TELEMETRY`**, **`MQTT_TOPIC_COMMAND`**, identifiants client, option **`MQTT_USE_AUTH`**. |
| **Télémétrie** | Publication JSON sur le topic télémétrie (structure alignée sur l’ancien `/api/state` : `sensors`, `weather`, `model_inputs`, `decision`, `manual`) ; **QoS retain** sur la publication pour faciliter les abonnés. |
| **Saisie manuelle** | Toujours possible en **série** (`A`, `C`, `S`, `H`) ; en plus, **abonnement** au topic **commande** : JSON `{"crop_age_days", "crop_idx", "soil_idx"}` (émis par le pont quand l’utilisateur valide le formulaire du dashboard PC). Variables globales **`g_crop_idx`**, **`g_soil_idx`**, **`g_crop_age_days`**, **`g_manual_ready`** (non `static`) pour le callback MQTT. |
| **Boucle** | **`mqtt_irrigation_loop()`** entre les cycles ; cadence capteurs / décision / publication typiquement **~5 s** ; pas de `WebServer` ni LittleFS dataset si les flags restent à 0. |
| **WiFi** | SSID / mot de passe dans **`weather_secrets.h`** (pas de WiFiManager dans ce croquis recréé — la §6.8 décrit une variante historique avec AP téléphone). |

##### Broker MQTT (ex. Mosquitto sur Windows)

- Fichier **`pc_irrigation_bridge/mosquitto.conf`** : **`listener 1883`**, **`allow_anonymous true`** (tests), **`persistence false`** — nécessaire sous **Mosquitto 2.x** pour accepter les clients **hors localhost** (sinon « local only » et l’ESP obtient `rc=-2` avec PubSubClient).
- Script **`apply_mosquitto_config.ps1`** (PowerShell **administrateur**) : copie la config vers `C:\Program Files\mosquitto\mosquitto.conf`, sauvegarde l’ancien, redémarre le service ; ne pas lancer un second `mosquitto -v` si le **service** occupe déjà le port **1883**.
- **Pare-feu** : autoriser le **TCP 1883 entrant** vers le PC qui héberge le broker.
- **Dépannage** : **`mosquitto` introuvable** → chemin complet `C:\Program Files\mosquitto\mosquitto.exe` ou ajout au **PATH** ; **MySQL erreur 10061** → serveur absent sur 3306 : démarrer **MySQL** dans **XAMPP** (ou service MySQL installé).
- Guide court : **`MOSQUITTO_WINDOWS.txt`**.

##### Côté PC (`pc_irrigation_bridge/`)

| Fichier | Rôle |
|---------|------|
| **`bridge.py`** | Client **paho-mqtt** (souscription télémétrie) ; serveur **Flask** (`/` → `dashboard.html`, **`GET /api/state`**, **`POST /api/manual`** → publish commande MQTT, **`GET /api/irrigation_log.csv`** → téléchargement du CSV). |
| **`dashboard.html`** | Interface locale (saisie, état, barres, cartes, bouton téléchargement CSV) ; libellés réduits (pas de bandeau MQTT détaillé dans la version actuelle). |
| **`db_mysql.py`** | **`TelemetryData`**, **`extract_telemetry_from_mqtt`**, **`telemetry_to_csv_row`** ; classe **`MySQLStore`** : **`CREATE DATABASE IF NOT EXISTS`** (nom validé `[A-Za-z0-9_]{1,64}`), **`CREATE TABLE`** `irrigation_telemetry` (`id`, `recorded_at`, champs alignés sur le CSV avec noms SQL sans `%`), **`insert`**. |
| **`requirements.txt`** | `flask`, `paho-mqtt` 1.x, **`pymysql`**. |
| **`data/irrigation_log.csv`** | Journal append à chaque message ; en-tête **réduit** aux colonnes réellement alimentées : `crop_name`, `soil_type`, `crop_age_days`, `temperature_C`, `humidity_%`, `rainfall_mm`, `wind_speed_m_s`, `soil_moisture_%`, `p_fraction`, `irrigate`, **`irrigation_litres`** (litres, **2 décimales** ; plus de nom `irrigation_amount_m3` dans ce fichier). |
| **Ligne de commande** | **`--reset-csv`** : sauvegarde `.bak.<timestamp>` et nouvel en-tête ; **`--no-mysql`** : désactive MySQL ; variables d’environnement **`MYSQL_*`** ou options **`--mysql-host`**, **`--mysql-user`**, etc. (base **`irrigation`**, table **`irrigation_telemetry`** par défaut). |
| **Schéma CSV** | Si la **première ligne** du fichier existant ne correspond plus à **`CSV_HEADER`**, le pont **renomme** l’ancien fichier en **`.bak.<timestamp>`** et recrée un CSV avec l’en-tête courant (réduction de colonnes, alignement dataset). |

##### Chaîne de données

1. L’ESP publie la télémétrie sur MQTT.  
2. Le pont reçoit le JSON, met à jour **`/api/state`**, append le **CSV**, exécute **`INSERT`** MySQL si activé.  
3. L’utilisateur envoie la saisie manuelle depuis le navigateur → **`POST /api/manual`** → message sur le topic commande → l’ESP met à jour la culture / sol / âge.

##### Vérification rapide

- Console **`bridge.py`** : **`[bridge] MySQL pret (base + table) : ...`** ou message d’erreur MySQL (ex. serveur arrêté, **XAMPP** : démarrer **MySQL** dans le panneau de contrôle).  
- **`SELECT COUNT(*) FROM irrigation.irrigation_telemetry`** après quelques cycles ESP.

##### Limites / production

- Le serveur Flask embarqué est le **mode développement** ; pour un produit multi-utilisateurs, prévoir **HTTPS**, **auth**, broker **TLS**, topics par **tenant**, et déploiement **WSGI** + MySQL managé (voir discussions produit hors de ce document).

---

*Document généré pour décrire l’état du projet et la stratégie multi-modèles ; les métriques du §2 proviennent d’une exécution de référence sur le CSV du dépôt avec les mêmes features que les notebooks. La section §6 décrit le déploiement ESP32 (SmallNN principal, premier arbre RF secondaire), PlatformIO, Arduino IDE, capteurs §6.6, météo zone §6.7, mini station météo §6.8 (`esp32_weather_station/` — Open-Meteo par défaut, capteurs locaux optionnels). Les **évolutions détaillées** du croquis irrigation, du mode web + journal LittleFS historiques et du **pont PC MQTT / CSV / MySQL** (**§6.10.10**) sont regroupées au **§6.10**.*
