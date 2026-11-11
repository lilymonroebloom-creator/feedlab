*** Settings ***
Documentation     Rejeu des sessions enregistrées : aucun message perdu, sortie cohérente,
...               rejeu déterministe. Les tests ne dépendent pas du contenu exact des sessions.
Library           feedlib.py


*** Test Cases ***
La session volatile est rejouée sans perdre un seul message
    [Tags]    smoke
    Replay    volatile
    Invariants Should Hold

La session calme est rejouée sans perdre un seul message
    Replay    calm
    Invariants Should Hold

Deux rejeux de la même session donnent la même sortie
    Replay    calm
    Replay Should Be Deterministic

Un rejeu accéléré donne la même sortie qu'un rejeu en rafale
    Replay    samples/sample.jsonl    speed=1000
    Invariants Should Hold
    Redacted Output Should Match    samples/expected_output.txt
