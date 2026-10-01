# Laya Computer V1 — implementação e validação

O novo plugin `laya-computer` implementa plano estruturado, execução local em etapas, decisões com `laya-mlx`, conexão persistente com Cua Driver, verificação de predicados, pausa/resgate e retomada versionada. O agente chamador continua responsável pelo planejamento e pelo resgate. Não há chamada interna a modelo remoto.

## Evidência real

Ambiente observado: macOS Apple Silicon; Cua Driver 0.23.2; `laya-mlx` 0.1.0; checkpoint `aac6fef/laya-typed-decisions-mlx`. O fluxo oficial de permissões confirmou Acessibilidade, Gravação de Tela e captura direta. O driver não foi atualizado nem colocado em modo de bypass.

Uma execução real no Photos selecionou **Months e depois Years**, com o modelo local escolhendo entre controles observados e o controlador verificando a seleção após cada ação:

| Medida desta execução | Resultado |
|---|---:|
| Etapas verificadas | 2 |
| Ações | 2 |
| Decisões Laya | 2 |
| Resgates solicitados | 0 |
| Reobservações por snapshot obsoleto | 0 |
| Tempo ativo do controlador | 17,313 s |
| Tempo nas duas inferências | 0,503 s |
| Carregamento/aquecimento local | 2,540 s |
| Tokens de entrada locais reportados | 158 |
| Tokens/custo do modelo forte | não medido |

O tempo ativo começa depois de iniciar/vincular o driver e inclui as observações, a inferência e as ações do controlador. Esta é uma observação única, não uma distribuição de latência ou comparação controlada. O plano reproduzível está em [`photos-navigation.json`](../../laya-computer/examples/photos-navigation.json). Rótulos dependem do idioma e versão do aplicativo.

**Esse resultado valida navegação local; não prova encontrar a foto mais antiga.** O benchmark econômico completo, incluindo planejamento, consultas de estado, resgate e subagentes, não foi medido.

## Limites encontrados no piloto da foto mais antiga

O Cua marcou a árvore do Photos como `elements_complete: false`, mesmo retornando todas as linhas enumeradas e após ampliar os limites da leitura. A grade visual não forneceu candidatos `AXImage` suficientes para demonstrar qual item era o primeiro da biblioteca. O contrato padrão pausa nesse estado. A opção explícita por observação parcial só admite predicados positivos sobre controles presentes e proíbe seleção ordinal; não serve para afirmar antiguidade global.

Uma tentativa inicial de navegação escolheu Months quando o plano pedia Years e retornou `rescue_needed` após uma ação; não houve falso sucesso. A apresentação das alternativas foi então alinhada à política de referência do Laya: critérios textuais curtos em vez de objetos JSON repetidos. Um teste separado acertou as três alternativas (Years, Months e All Photos); depois ocorreu a execução de duas etapas acima. Esses ensaios de desenvolvimento não estimam acurácia geral.

A inspeção encontrou opções distintas de ordenar por data de adição e por data de captura. Clicar um item de menu como se pertencesse à janela retornou `element_outside_target_window`; o adaptador passou a construir o caminho nativo a partir dos ancestrais observados e usar `invoke_menu`. O driver aceitou a invocação da ordenação por captura, mas aceitação sem verificação não demonstra o resultado final.

Também houve uma tentativa AX de selecionar All Photos recusada pelo driver com erro `AXUIElementPerformAction(AXPress) -25205`. Inspeções e intervenções manuais posteriores por Cua são diagnóstico do modelo forte, não execução autônoma atribuída ao Laya. Imagens e conteúdo pessoal da biblioteca não foram incluídos no repositório.

O objetivo completo “foto mais antiga, data conferida” permanece **não validado pelo MCP**. O servidor entrega resgate explícito quando faltam evidências, em vez de declarar conclusão.

## Validação automatizada e pacote

- 41 testes do plugin passaram: fixtures sintéticas de planos, efeitos incertos, snapshots obsoletos, observação parcial, retomada, concorrência, parada e transporte. Incluem revalidação de precondições após snapshot obsoleto, limite de tempo durante observação e liberação de capacidade após falhas. O teste de protocolo inicia o servidor real por stdio, lista ferramentas e rejeita um plano inválido, sem acessar a UI.
- 15 testes de scripts do catálogo passaram; `check_pins.py` confirmou o pin do navegador existente.
- Ruff passou; wheel e sdist foram construídos.
- Handshake MCP real confirmou `inspect`, `run_plan`, `get_run`, `resume_plan` e `stop_run`.
- CI adicionada para testes portáveis em Ubuntu e macOS, sem carregar pesos nem acessar bibliotecas pessoais.

Não confundir os testes sintéticos com a evidência real de navegação acima. CI também não comprova o piloto do Photos: ele depende do macOS, das permissões e da cobertura do driver instalado.
