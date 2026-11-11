*** Settings ***
Documentation     Tests métamorphiques : chaque mutation d'une session enregistrée doit décaler
...               les compteurs exactement de l'écart attendu, sur les deux sessions.
...               L'écart de la colonne ECART ATTENDU est aussi comparé à celui que calcule l'opérateur.
Library           feedlib.py
Test Template     Chaque session réagit à la mutation par l'écart attendu


*** Variables ***
@{SESSIONS}       calm    volatile


*** Test Cases ***                                                     MUTATION                  ECART ATTENDU
Un message perdu crée exactement un gap                                drop                      messages_in=-1 quotes=-1 gaps=+1
Un message reçu deux fois est compté comme doublon                     duplicate                 messages_in=+1 duplicates=+1
Un message en retard crée un gap puis un skip                          delay                     quotes=-1 gaps=+1 out_of_seq=+1
Deux messages inversés du même symbole créent un gap puis un skip      reorder_same_symbol       quotes=-1 gaps=+1 out_of_seq=+1
Deux messages inversés de symboles différents ne changent rien         reorder_cross_symbol      aucun
Un prix illisible est rejeté sans créer de faux gap                    corrupt_price_abc         quotes=-1 rejects=+1
    [Tags]    smoke
Un prix nul est rejeté                                                 corrupt_price_zero        quotes=-1 rejects=+1
Un prix négatif est rejeté                                             corrupt_price_negative    quotes=-1 rejects=+1
Une quantité négative est rejetée                                      corrupt_qty_negative      quotes=-1 rejects=+1
Un champ manquant est rejeté sans créer de faux gap                    corrupt_missing_field     quotes=-1 rejects=+1
Un numéro de séquence illisible n'est pas consommé et crée un gap     corrupt_missing_u         quotes=-1 rejects=+1 gaps=+1
Un carnet croisé est rejeté                                            cross                     quotes=-1 rejects=+1
Une dernière ligne coupée est rejetée                                  truncate_last             quotes=-1 rejects=+1
Une rafale ne change que le rythme                                     burst                     aucun


*** Keywords ***
Chaque session réagit à la mutation par l'écart attendu
    [Arguments]    ${mutation}    ${ecart_attendu}
    FOR    ${session}    IN    @{SESSIONS}
        Replay    ${session}
        Apply Mutation    ${mutation}
        Stats Delta Should Be    ${ecart_attendu}
        Invariants Should Hold
    END
