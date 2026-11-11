*** Settings ***
Documentation     Petits fichiers de référence : l'échantillon écrit à la main, avec ses défauts
...               glissés exprès, doit produire exactement le corrigé (après caviardage).
Library           feedlib.py
Library           Process
Library           OperatingSystem


*** Variables ***
${ROOT}           ${CURDIR}/..
${PYTHON}         ${{sys.executable}}


*** Test Cases ***
La sortie de l'échantillon correspond au corrigé
    [Tags]    smoke
    Replay    sample
    Redacted Output Should Match    samples/expected_output.txt
    Invariants Should Hold

Le script de comparaison accepte la sortie brute de l'échantillon
    ${out}=    Set Variable    ${OUTPUT DIR}/sample_raw_output.txt
    ${dump}=    Run Process    ${PYTHON}    -m    feedlab.dump    ${ROOT}/samples/sample.jsonl    --out    ${out}
    ...    cwd=${ROOT}    env:PYTHONPATH=${ROOT}
    Should Be Equal As Integers    ${dump.rc}    0    ${dump.stderr}
    ${cmp}=    Run Process    bash    ${ROOT}/scripts/compare.sh    ${out}    ${ROOT}/samples/expected_output.txt
    Should Be Equal As Integers    ${cmp.rc}    0    ${cmp.stdout}

Le script de comparaison signale un prix modifié
    ${out}=    Set Variable    ${OUTPUT DIR}/sample_altered_output.txt
    ${dump}=    Run Process    ${PYTHON}    -m    feedlab.dump    ${ROOT}/samples/sample.jsonl    --out    ${out}
    ...    cwd=${ROOT}    env:PYTHONPATH=${ROOT}
    ${text}=    Get File    ${out}
    ${altered}=    Replace String Once    ${text}    bid=67000.15    bid=67000.16
    Create File    ${out}    ${altered}
    ${cmp}=    Run Process    bash    ${ROOT}/scripts/compare.sh    ${out}    ${ROOT}/samples/expected_output.txt
    Should Be Equal As Integers    ${cmp.rc}    1
    Should Contain    ${cmp.stdout}    +QUOTE sym=BTCUSDT u=102 bid=67000.16


*** Keywords ***
Replace String Once
    [Arguments]    ${text}    ${old}    ${new}
    ${result}=    Evaluate    $text.replace($old, $new, 1)
    RETURN    ${result}
