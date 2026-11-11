*** Settings ***
Documentation     Suite de latence (tag perf, exclue par défaut) : percentiles, médiane de
...               plusieurs exécutions, comparaison à baselines/latency.json, seuil de 10 %.
...
...               Robot ne lit pas de fichier de configuration : pour que la commande standard
...               l'exclue, la suite est sautée (SKIP) tant que PERF n'est pas à True.
...               Lancer : robot --pythonpath . --outputdir results --variable PERF:True --include perf robot/
Library           feedlib.py
Test Tags         perf
Test Setup        Skip If    not ${PERF}    suite perf exclue par défaut (ajouter --variable PERF:True)


*** Variables ***
${PERF}           ${False}


*** Test Cases ***
Le détecteur repère un délai artificiel
    Injected Delay Should Be Detected

La latence en rafale ne régresse pas
    Latency Should Not Regress    burst

La latence à vitesse réelle ne régresse pas
    Latency Should Not Regress    realtime
