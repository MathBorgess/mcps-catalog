# Laya Computer MCP — proposta V1

Status: implementação autorizada pelo usuário e realizada como `laya-computer`; validação e limitações em [relatório](../reports/laya-computer-v1-validation.md). Decisão de criar um novo MCP: [ADR 0001](../adr/0001-separate-desktop-decision-mcp.md). Evidências: [investigação](2026-09-29-laya-cua-investigation.md).

## Objetivo e escolhas confirmadas

Economizar tokens do modelo forte por tarefa concluída. O modelo forte antecipa o plano inteiro ou etapas previstas; o executor local avança entre essas etapas sem chamar o planejador a cada ação ou transição. Laya resolve escolhas delimitadas e Cua Driver observa/executa. Lentidão é aceitável; resgates frequentes e alto consumo do modelo forte significam que a proposta não atingiu seu objetivo.

A V1 terá resgate pelo modelo forte, que resolve o obstáculo e altera o mínimo viável do plano. O chamador pode delegar essa intervenção a um subagente. Modelos textuais menores não são necessários na V1. O servidor será um plugin novo no catálogo, preservando a independência dos plugins existentes.

## Piloto: foto mais antiga no Photos

Objetivo: localizar e abrir uma foto com a menor data registrada no escopo acessível da biblioteca aberta, pela ordenação cronológica, e conferir sua data no painel Informações. Não é necessário escolher um ano previamente.

Plano semântico proposto:

1. Observar/abrir Photos e identificar a biblioteca, a visualização e filtros relevantes.
2. Usar a biblioteca cronológica, evitando confundir data de captura com data de importação. Explicitar no resultado se o escopo é pessoal, compartilhado ou ambos.
3. Remover filtros restritivos de data/álbum na visualização; restringir a fotos quando necessário para não escolher um vídeo como resultado. Não criar ou modificar álbuns.
4. Observar se existe ordenação da mais antiga para a mais recente e aplicá-la quando suportada. Se a biblioteca já estiver cronológica, navegar ao começo. Não pressupor que o menu de ordenação de álbuns exista na biblioteca geral.
5. Selecionar o primeiro candidato do início cronológico, abrir suas informações e ler a data registrada.
6. Verificar escopo, ordenação/início e data. Retornar um resumo compacto com a evidência textual disponível; empates admitem qualquer foto na menor data.

Uma data antiga sozinha não prova que a foto seja a mais antiga. Sem evidência suficiente do início da ordenação ou do escopo, devolver verificação incompleta. Biblioteca vazia é um resultado distinto de foto encontrada. Registros ocultos, apagados, outras bibliotecas não abertas e itens indisponíveis não entram em uma alegação global de antiguidade. Não alterar fotos, metadados, sincronização ou compartilhamento.

Fontes oficiais:

- [Apple: encontrar fotos por data](https://support.apple.com/en-gb/guide/photos/pht56eafa987/mac).
- [Apple: ordenar conteúdo de álbuns](https://support.apple.com/guide/photos/create-and-work-with-albums-pht6d60a1f1/mac).
- [Apple: painel Informações](https://support.apple.com/guide/photos/see-photo-and-video-information-phta8b25fa42/mac).

As opções e rótulos da versão instalada precisam de observação. Na investigação inicial, o driver retornou `permissions_pending`. Durante a implementação, o fluxo oficial confirmou Acessibilidade, Gravação de Tela e captura direta; a limitação seguinte passou a ser a cobertura de acessibilidade do Photos. Nenhuma foto foi localizada ou verificada nesta investigação.

## Contrato de plano proposto

Plano estruturado, validado antes de executar: versão, objetivo, app/escopo permitido, etapas identificadas, condições de entrada, operações permitidas, alvos semânticos, valores explícitos, condições de conclusão e alternativas previstas. Cada etapa aponta sua próxima etapa ou fim. As condições suportadas são enumeradas pelo servidor; texto livre não vira código executável.

O modelo forte antecipa intenções, condições e alternativas comuns. IDs e tokens de controles são obtidos apenas da observação atual. O controlador guarda a etapa e o progresso; Laya associa requisitos a candidatos observados ou responde perguntas de escolha. A política não pede a Laya planejamento geral nem geração arbitrária de ferramentas.

Controles iniciais: selecionar/clicar um candidato, abrir um candidato, preencher um valor explícito quando suportado, acionar atalhos predefinidos de navegação, rolar, esperar de forma limitada e verificar predicados observáveis. A implementação só habilita operações com contrato verificado no driver. Sem shell arbitrário, código gerado ou interpretação de pixels pelo Laya.

## Componentes e ferramentas MCP propostas

- **Controlador de plano:** progresso, transições, limites, verificação e pausa/resgate.
- **Decisor Laya:** biblioteca `laya-mlx` com carregamento persistente e checkpoint concreto configurável; padrão inicial igual ao projeto existente. Reutilizar inferência e validação de escolhas; não copiar a política web inteira.
- **Adaptador Cua:** normaliza observação em candidatos compactos e traduz escolhas em ações com tokens atuais. A primeira implementação usa a interface MCP do driver por conexão persistente; o transporte precisa de teste de compatibilidade com a instalação.
- **Servidor MCP:** recebe planos, controla sessões e retorna resultados compactos. Não escolhe nem autentica um provedor de modelo forte: planejamento e resgate pertencem ao agente chamador.

Ferramentas propostas: `inspect` (estado inicial compacto), `run_plan` (criar sessão e executar), `resume_plan` (retomar com reparo e versão esperada), `get_run` (progresso/evidências) e `stop_run` (interromper). Execuções longas usam um identificador e consulta de estado, sem depender de uma única chamada MCP permanecer aberta indefinidamente. Consultar estado não reobserva a janela nem invalida tokens por si só.

Resultados: `running`, `completed`, `rescue_needed`, `blocked`, `stopped`. Verificação é um campo separado: satisfeita, não satisfeita ou desconhecida. Um clique aceito não equivale a etapa concluída; confiança do modelo não é um verificador calibrado.

Limites implementados, configuráveis para baixo: 60 ações e 5 minutos de execução ativa acumulados por sessão, excluindo a pausa de resgate; até um resgate. Uma falha de verificação pausa imediatamente, enquanto snapshots obsoletos admitem reobservação limitada. Atingir limite nunca é sucesso e a retomada não reinicia o orçamento. Execuções recuperadas são medidas separadamente das autônomas.

## Resgate e concorrência

Ao pausar, devolver etapa atual, motivo, estado relevante, ações recentes com efeitos conhecidos e progresso verificado. Instrução: resolver o impedimento, preservar objetivo/restrições e modificar apenas o trecho necessário do plano. O modelo forte pode fazer uma intervenção pontual ou delegá-la. Sua escolha de modelo e os tokens de subagentes precisam ser registrados pelo chamador.

Nenhuma ação local continua durante o resgate. Retomar com nova observação e a versão esperada do plano, revalidando precondições afetadas. Preservar etapas concluídas que continuem válidas. Não repetir automaticamente ações com efeito incerto após timeout.

Uma sessão ativa por aplicativo dentro do servidor, uma restrição conservadora que também evita disputar janelas do mesmo app. Outros clientes do Cua podem invalidar tokens mesmo assim: rejeição de snapshot obsoleto pede nova observação, sem inventar identificadores. O servidor mantém tokens reais no executor e oferece ao decisor apenas candidatos referenciáveis.

Interfaces sem semântica textual suficiente, efeitos não verificáveis e bloqueios de permissões levam a resgate ou bloqueio explícito. A V1 não exige adicionar outro modelo para percepção visual; o agente de resgate pode usar suas ferramentas disponíveis.

## Avaliação e entrega

Comparar modelo forte conduzindo as interações com modelo forte planejando e execução delegada, usando a mesma tarefa, escopo e estado inicial documentado. Contar planejamento, inspeção inicial, consultas de estado, resgates, subagentes e resposta final. Separar tokens de entrada/saída/cache quando disponíveis; se o cliente não expuser consumo, registrar **não medido**, sem inferir tokens a partir de tamanho de JSON como se fossem cobrança real.

Registrar sucesso verificado, chamadas intermediárias ao forte, ações locais, resgates, tempo frio/aquecido e custos conhecidos. Repetir a partir de um estado comparável; não confundir execução manual da investigação com execução pelo Laya. Benchmarks Jev orientam cenários e hipóteses, mas não substituem a medição desse fluxo.

Implementar no layout do catálogo: diretório do plugin com pacote, manifesto, README, testes e entrada de marketplace. Testes unitários de controlador/contratos usam observações sintéticas claramente rotuladas; integração real usa Cua + Laya + Photos e depende das permissões macOS. Testar plano inválido, conclusão sem evidência, snapshot obsoleto, mutação incerta, retomada e limites. Fixtures não comprovam cobertura do Photos.

Seguir CI e validações do catálogo e abrir PR contra `main` quando a implementação estiver pronta, sem push direto a `main`. Não introduzir provisionamento cloud para um piloto Apple Silicon local.

## Ajuste após observação real

O Cua 0.23.2 marcou a árvore do Photos como incompleta mesmo com todas as linhas enumeradas retornadas. O padrão continua recusando esse estado. Etapas podem optar por `allow_partial_observation` apenas para verificar fatos positivos de controles presentes; alvos por primeiro/último/posição ficam proibidos, e ausência continua desconhecida. Isso permite validar navegação sem afirmar que o primeiro item observado é o mais antigo da biblioteca. O decisor passou a receber critérios textuais curtos, seguindo a política de referência do Laya.
