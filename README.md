# feedlab

**feedlab est un petit feed handler de market data (format Binance bookTicker) accompagné d'une stratégie de test qui ne demande presque jamais d'écrire des données de test à la main.**

Le handler est volontairement simple. L'intérêt du projet est sa stratégie de test, qui combine cinq méthodes :

1. **A.** des enregistrements rejoués ;
2. **B.** des mutations (tests métamorphiques) ;
3. **C.** des tests basés sur les propriétés ;
4. **D.** des comparaisons entre versions (tests différentiels) ;
5. **E.** de petits fichiers de référence.

S'y ajoutent une suite de latence et des tests unitaires classiques.

## Le handler en bref

Entrée : une ligne JSON par message.

```json
{"recv_ts_ns": 1700000030000000000, "raw": {"stream": "btcusdt@bookTicker", "data": {"u": 101, "s": "BTCUSDT", "b": "67000.10", "B": "0.5", "a": "67000.20", "A": "1.2"}}}
```

Règles, par symbole (`dernier` = dernier numéro de séquence consommé) :

| Cas | Événement | Compteur |
|---|---|---|
| `u = dernier + 1` et message valide | `QUOTE`, le top of book est mis à jour | `quotes` |
| `u > dernier + 1` | `GAP expected=… received=…`, puis le message est traité (resynchronisation) | `gaps` |
| `u < dernier` | `SKIP` : un prix ancien n'écrase jamais un prix récent | `out_of_seq` |
| `u = dernier` | `DUPLICATE` | `duplicates` |
| JSON invalide, champ manquant, prix non numérique, nul ou négatif, quantité négative, bid ≥ ask | `REJECT reason=…` | `rejects` |

Ordre de décision :
1. Si le symbole ou `u` est illisible, c'est un REJECT, et **aucun numéro n'est consommé**.
2. Sinon, les règles DUPLICATE et SKIP s'appliquent.
3. Sinon, un GAP est émis si besoin.
4. Enfin, le contenu est validé. Qu'il donne QUOTE ou REJECT, **`u` est consommé**, si bien qu'un message rejeté ne crée pas de faux GAP sur le message suivant.

Loi de conservation : `messages_in = quotes + out_of_seq + duplicates + rejects`. Le GAP est un événement en plus ; il ne décide pas du sort d'un message.

Sortie abonné : une ligne `clé=valeur` par événement. Elle se termine par trois champs qui changent à chaque exécution : `proc_ts` (19 chiffres), `host` et `latency_ns`.

```
QUOTE sym=BTCUSDT u=101 bid=67000.10 bid_qty=0.5 ask=67000.20 ask_qty=1.2 proc_ts=1762858123456789012 host=debian latency_ns=4210
```

Le **caviardage** (`feedlab/redact.py`) masque uniquement trois choses : `proc_ts=` suivi d'exactement 19 chiffres, `host=…` et `latency_ns=…`. Il ne touche jamais un prix, un symbole ou un numéro de séquence. Un horodatage abîmé (18 chiffres) reste visible, et la comparaison échoue donc.

| Module | Rôle |
|---|---|
| `feedlab/parser.py` | ligne brute → message ou raison du rejet ; ne lève jamais d'exception ; prix en `Decimal` |
| `feedlab/book.py` | top of book par symbole, règle bid < ask |
| `feedlab/handler.py` | règles de séquence, compteurs, sortie abonné, délai artificiel optionnel |
| `feedlab/replay.py` | rejeu à vitesse réelle (`--speed 1`), accélérée (`--speed N`) ou maximale (`--speed 0`) |
| `feedlab/dump.py` | écrit la sortie abonné d'une session |
| `feedlab/redact.py` | caviardage |
| `feedlab/differential.py` | comparaison caviardée de deux sorties |
| `feedlab/latency.py` | mesure, percentiles, baseline, détection de régression |

## D'où viennent les données, et comment on sait que c'est juste

| Méthode | D'où viennent les données | Comment on sait que la sortie est juste (l'oracle) | Ce que la méthode couvre |
|---|---|---|---|
| A. Rejeu d'enregistrements | `recordings/*.jsonl`, générés avec une graine par `testdata/generate_session.py` | Des lois vraies pour n'importe quelle session : conservation des messages, un événement final par message, déterminisme, vitesse sans effet | Volume réaliste, absence de perte, stabilité |
| B. Mutations (métamorphique) | Les mêmes enregistrements, modifiés par `testdata/mutate.py` (avec graine) | Relation : compteurs(muté) = compteurs(propre) + delta, le delta étant calculé par une formule propre à chaque opérateur ; `burst` ne change pas la sortie caviardée | Message perdu, dupliqué, en retard, inversé, abîmé, carnet croisé, ligne coupée, rafale |
| C. Propriétés (Hypothesis) | Générées par Hypothesis (messages valides, mal formés, flux mélangés) | Des invariants : pas d'exception, prix reproduit à l'identique, bid < ask, `u` croissant par symbole, conservation | Cas limites que personne n'aurait écrits |
| D. Différentiel | Toutes les sessions, rejouées par deux versions du code (git worktree) | La version de référence : toute différence de sortie caviardée est signalée | Régression entre versions, même sur un comportement non spécifié |
| E. Fichiers de référence | `samples/sample.jsonl`, une douzaine de lignes écrites à la main avec des défauts glissés exprès | `samples/expected_output.txt`, le corrigé écrit à la main | Lisibilité, exemple concret de chaque règle |

## Risques × tests

| Risque | Tests qui l'attrapent |
|---|---|
| Prix faux | C `test_valid_price_is_reproduced_exactly` ; E `test_sample_matches_expected_output` ; unitaire `test_price_string_is_kept_exactly` ; D (toute différence de prix) |
| Message abîmé | B `corrupt_*`, `truncate_last` ; C `test_parser_never_raises`, `test_malformed_message_is_rejected_never_quoted` ; unitaires `tests/unit/test_parser.py` |
| Message perdu | A `test_no_message_is_lost` ; B `drop` ; unitaire `test_gap_is_reported_then_message_accepted` |
| Message en retard | B `delay`, `reorder_same_symbol` ; unitaires `test_late_message_is_skipped_and_never_overwrites`, `test_delayed_99_after_101_gives_gap_then_skip` ; C `test_book_never_goes_back_in_time` |
| Message dupliqué | B `duplicate` ; unitaire `test_duplicate` ; E (ligne DUPLICATE du corrigé) |
| Carnet croisé | B `cross` ; C `test_every_quote_has_bid_below_ask` ; unitaire `test_crossed_book` ; D `test_monkeypatched_rule_is_detected` |
| Faux GAP après un rejet | unitaire `test_rejected_message_consumes_its_sequence_number` ; B `corrupt_price_abc` (delta sans GAP) ; Robot « Un prix illisible est rejeté sans créer de faux gap » |
| Sortie non conforme | E `test_sample_matches_expected_output` + `scripts/compare.sh` ; unitaires `tests/unit/test_redact.py` |
| Régression entre versions | D `tools/differential.py`, `test_cli_detects_a_patched_candidate` |
| Régression de latence | perf `test_latency_not_regressed_against_baseline` ; détecteur `test_injected_delay_is_detected` |

## Commandes

Toutes les commandes se lancent depuis la racine du projet.

### Installation

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
source .venv/bin/activate
```

### Générer les enregistrements (même graine = même fichier à l'octet près)

```bash
python -m testdata.generate_session --all
```

### Appliquer une mutation à la main

```bash
python -m testdata.mutate recordings/session_calm.jsonl --op delay --seed 1 --out results/mutated.jsonl
```

### Rejouer une session et afficher les compteurs

```bash
python -m feedlab.dump samples/sample.jsonl --stats -
```

### Tests pytest (sans la latence)

```bash
pytest
```

### Tests pytest de latence seulement

```bash
pytest -m perf
```

### Tous les tests pytest, latence comprise

```bash
pytest -m "perf or not perf"
```

### Une seule méthode (method_a … method_e, unit, testdata)

```bash
pytest -m method_b
```

### Lister les tests

```bash
pytest --collect-only -q
```

### Robot Framework (toutes les suites, latence sautée)

```bash
robot --pythonpath . --outputdir results robot/
```

### Robot Framework, scénarios smoke

```bash
robot --pythonpath . --outputdir results --include smoke robot/
```

### Robot Framework, suite de latence

```bash
robot --pythonpath . --outputdir results --variable PERF:True --include perf robot/
```

### Test différentiel contre une version de référence

```bash
python tools/differential.py --baseline HEAD~1
```

### Comparer une sortie au corrigé avec compare.sh

```bash
mkdir -p results
python -m feedlab.dump samples/sample.jsonl --out results/sample_output.txt
scripts/compare.sh results/sample_output.txt samples/expected_output.txt
```

### Vérifier la latence par rapport à la baseline

```bash
python -m feedlab.latency --check
```

### Voir le détecteur réagir à un délai artificiel

```bash
python -m feedlab.latency --check --scenario burst --inject-delay-us 200
```

### Mettre à jour la baseline de latence

```bash
python -m feedlab.latency --update-baseline
```

### Régénérer le tutoriel HTML

```bash
python tools/build_tutorial.py
```

Codes retour : `tools/differential.py` et `scripts/compare.sh` renvoient 0 si les sorties sont identiques, 1 s'il y a une différence (le diff est affiché) et 2 en cas d'erreur d'utilisation. `python -m feedlab.latency --check` renvoie 1 en cas de régression.

## La latence

- **Mesure** : `time.perf_counter_ns()` entre l'injection d'un message dans le handler et l'émission de sa ligne de sortie.
- **Statistiques** : p50, p99 et p99.9 (rang le plus proche). La moyenne n'est jamais utilisée seule.
- **Scénarios** :
  - `burst` : la session volatile complète à `--speed 0` ;
  - `realtime` : les 1 000 premiers messages de la session calme à `--speed 1`, avec une attente active plutôt que `sleep` pour garder le cœur « chaud ».
- **Réduction du bruit** : 5 exécutions précédées d'un échauffement. On garde la **médiane** de chaque percentile.
- **Seuil** : échec si la métrique se dégrade de plus de 10 %. Le seuil se règle avec `--threshold` ou `FEEDLAB_LATENCY_THRESHOLD`.
- **Test du détecteur** : `--inject-delay-us` (ou `FeedHandler(inject_delay_ns=…)`) ajoute une attente active. Le test mesure d'abord la machine, puis injecte 3 × p99 + 100 µs : il ne dépend donc d'aucun chiffre absolu.
- **Exclusion par défaut** : marker pytest `perf`, exclu par `addopts`. Dans Robot, le tag `perf` est complété par un SKIP tant que `PERF` n'est pas à True.

## Limites et simplifications assumées

- **Numéros de séquence contigus par symbole.** C'est une simplification : chez Binance, `u` est un identifiant de mise à jour du carnet, pas un compteur contigu par flux.
- **Enregistrements simulés.** `recordings/` est produit par un générateur (marche aléatoire, rafales, quelques anomalies naturelles). Ce ne sont pas de vraies captures : les microstructures réelles (rafales corrélées entre symboles, reconnexions, messages de carnet profond) n'y sont pas.
- **Top of book seulement.** Pas de profondeur de carnet, pas de snapshot ni de resynchronisation par snapshot : après un GAP, on accepte simplement le message.
- **Champ `stream` non vérifié.** Sa cohérence avec `s` n'est pas contrôlée.
- **Parser strict.** Les nombres doivent être des chaînes au format décimal canonique : `1e5`, `01.5`, `+1`, `NaN` et les nombres JSON (qui passeraient par un float) sont rejetés. Une quantité 0 est acceptée.
- **Latence mesurée en Python, dans un seul processus**, sans réseau ni noyau : ce sont des microsecondes Python, pas celles d'un handler C++ en kernel bypass.
- **Bruit de la VM.** Sur la machine virtuelle de développement, le p99 en temps réel varie de +16 % à +115 % d'une série à l'autre sans aucun changement de code (préemption par l'hyperviseur). Le scénario `realtime` bloque donc sur le **p50**, qui reste stable ; son p99 est affiché pour information. Le scénario `burst` bloque bien sur le p99.
- **Opérateurs de mutation.** Ils ne visent qu'un message dans un « voisinage propre » (message en séquence, suivants contigus et valides), pour que le delta reste une formule exacte. `truncate_last` refuse une session dont la dernière ligne n'est pas une cotation en séquence. Le contrôle de validité de `testdata/mutate.py` reprend volontairement, en plus simple, les règles du parser.
- **Hypothesis en mode `derandomize=True`.** Les mêmes exemples reviennent à chaque exécution, ce qui garantit la reproductibilité mais explore moins qu'un mode aléatoire.
- **Différentiel.** La version de référence doit déjà contenir la commande `python -m feedlab.dump` (c'est le cas depuis le premier commit). Les sessions rejouées sont celles de l'arbre courant.
- **Robot ne lit aucun fichier de configuration.** L'exclusion de la latence par défaut passe donc par un `Skip If`.
- **Hors périmètre** : CI, Jenkins, exécutions planifiées.
