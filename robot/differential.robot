*** Settings ***
Documentation     Tests différentiels : la version courante est rejouée contre une version de
...               référence (git) sur toutes les sessions ; toute différence est signalée.
Library           feedlib.py
Suite Teardown    Remove Temporary Candidates


*** Test Cases ***
La version courante produit la même sortie que la dernière version commitée
    ${rc}=    Run Differential    baseline=HEAD
    Should Be Equal As Integers    ${rc}    0

Une règle modifiée exprès est détectée par le différentiel
    ${candidate}=    Make Candidate With Patched Rule
    ${rc}=    Run Differential    baseline=HEAD    candidate=${candidate}
    Should Be Equal As Integers    ${rc}    1
