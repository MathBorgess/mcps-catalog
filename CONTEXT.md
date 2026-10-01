# MCPs pessoais e automação assistida

Vocabulário do catálogo e da investigação de automação com decisões locais.

## Linguagem

**Plano de tarefa**: Objetivo e etapas antecipados pelo planejador forte, com condições observáveis para reconhecer progresso e conclusão. Deve permitir que a execução delegada avance por várias etapas sem solicitar novo planejamento a cada uma.

**Etapa verificável**: Parte do plano cujo resultado pode ser reconhecido no ambiente. Sua conclusão permite avançar localmente para a próxima etapa já prevista.

**Planejador forte**: Modelo responsável pelo raciocínio sobre o objetivo e pela elaboração do plano que orienta o trabalho delegado.

**Decisor delegado**: Componente que resolve escolhas intermediárias necessárias para avançar no plano do planejador forte. Laya é o decisor tipado inicialmente considerado; modelos textuais menores também fazem parte da direção pretendida.

**Observador**: Capacidade que apresenta ao sistema o estado relevante do ambiente para orientar uma decisão ou verificar um resultado.

**Executor**: Capacidade que realiza uma operação no ambiente a partir de uma decisão. Uma mesma ferramenta pode oferecer observação e execução.

**Decisão tipada**: Escolha entre alternativas delimitadas para uma pergunta específica sobre o estado observado.
_Evitar_: Usar decisão tipada como sinônimo de planejamento autônomo.

**Candidato observado**: Controle ou alternativa de interação identificado em uma observação da interface.

**Condição de conclusão**: Resultado observável necessário para considerar uma tarefa concluída. Uma declaração de conclusão do modelo não constitui, sozinha, verificação independente.

**Resgate**: Intervenção do modelo forte quando a execução local não consegue progredir. Na V1, resolve o obstáculo e adapta o mínimo necessário do plano, preservando o objetivo e o progresso válido; pode envolver um subagente.
