# Maxx Nexus — estrutura da mesa de operação e estratégia

## Estado atual

O Maxx Nexus é uma camada de decisão **PAPER-only**, determinística, persistente e auditável. Ele não contém executor de ordens, credenciais Binance, agendamento automático, alavancagem, margem, futuros ou conexão com dinheiro real.

Modos:

- `OFF` — compatibilidade total com o simulador baseline;
- `AUDIT` — cria pareceres, Conselho e gates em sombra, sem alterar a decisão baseline;
- `ENFORCE` — falha fechado; uma entrada precisaria de todos os gates aprovados e de um permit PAPER de uso único.

O padrão é `OFF`. A primeira ativação segura é `AUDIT`. A promoção `OFF → AUDIT → ENFORCE` fica persistida no banco; qualquer downgrade no mesmo banco falha fechado e exige um banco PAPER novo. No estado atual, `ENFORCE` bloqueia entradas porque fontes externas obrigatórias ainda estão explicitamente `NOT_CONFIGURED`.

## Organograma

### Inteligência

| Cargo | Responsabilidade | Fase atual |
|---|---|---|
| Pesquisador de Mercado | Novas oportunidades e tendências | `NOT_CONFIGURED` |
| Analista Fundamentalista | Projeto, tokenomics, equipe e fundamentos | `NOT_CONFIGURED` |
| Analista Técnico | Gráfico, tendência, indicadores e sinal | Implementado com dados locais |
| Analista Quantitativo | Modelo, calibração, suporte e incerteza | Implementado com dados locais |
| Analista On-chain | Movimentações e métricas de blockchain | `NOT_CONFIGURED` |
| Analista Macro | Juros, inflação e cenário global | `NOT_CONFIGURED` |
| Analista de Notícias | Notícias, eventos e riscos informacionais | `NOT_CONFIGURED` |
| Analista de Mercado | Liquidez, spread, regime e contexto | `NOT_CONFIGURED` |

Fonte ausente nunca é transformada em parecer neutro ou favorável: o papel retorna `ABSTAIN` com `EXTERNAL_SOURCE_NOT_CONFIGURED`.

### Controle

| Cargo | Responsabilidade | Gate |
|---|---|---|
| Analista de Compliance | Confirma PAPER-only, fontes permitidas e limites | `COMPLIANCE` |
| Risk Manager | Supervisiona stop, trava diária e política de risco | `RISK` |
| Analista de Risco | Valida plano, exposição e relação entrada/stop/alvo | `RISK` |
| Gestor de Capital | Confere caixa, capacidade e concentração | `CAPITAL` |

### Mesa

| Cargo | Responsabilidade | Estado |
|---|---|---|
| Head Trader | Aceita somente decisão aprovada pelo Conselho e gates | Formalizado |
| Senior Trader | Confere execução estratégica PAPER | Formalizado |
| Junior Trader | Executa somente sob o fluxo aprovado | Formalizado |
| Scalper Trader | Curtíssimo prazo | Desabilitado |

### Auditoria

O Auditor Financeiro confere registros, vínculo entre review/permit/posição e resultado pós-operação. Posições anteriores ao Nexus recebem `LEGACY_NOT_APPLICABLE`; posições vinculadas a permit recebem auditoria `VERIFIED` no fechamento.

## Fluxo obrigatório

1. Validar candle fechado e integridade dos dados.
2. Gerenciar posições abertas e executar saídas protetivas.
3. Maturar labels prospectivos do modelo.
4. Identificar candidatos técnicos elegíveis.
5. Aplicar o gate de probabilidade, quando configurado.
6. Produzir pareceres dos analistas.
7. Consolidar o Conselho.
8. Aplicar Compliance, Risco, Capital e Mesa.
9. Em `AUDIT`, registrar a decisão em sombra e manter o baseline.
10. Em `ENFORCE`, bloquear ou emitir um permit PAPER de uso único.
11. Consumir o permit atomicamente ao abrir a posição simulada.
12. Auditar o resultado ao encerrar a posição.

Nenhum gate de entrada pode impedir uma saída protetiva.

## Controles de capital e lucro

- banca padrão: 100 USDT simulados;
- exposição máxima: 10% do patrimônio por posição;
- máximo: três posições;
- stop rígido: 5% no preço;
- risco teórico baseline: aproximadamente 0,5% do patrimônio por posição antes de custos/gaps;
- trava diária: 3% de perda de patrimônio UTC, bloqueando apenas novas entradas;
- trailing stop: 3% abaixo do pico;
- profit lock: ativação padrão após +4% de pico e piso líquido estimado de +0,5%, após fees/slippage configurados.

O profit lock não garante resultado: em um gap, a referência PAPER é a abertura abaixo do stop. Quando o mesmo candle alcança a ativação e fecha abaixo do novo piso, o simulador reconhece o cruzamento definitivo e encerra no novo stop; quando apenas a mínima fica abaixo e o fechamento recupera, a ordem intradiária é ambígua e o stop novo passa a valer no candle seguinte. A simulação não conhece a sequência intradiária completa de um candle diário.

## Probabilidade calibrada

O threshold padrão não é promessa de acerto:

- probabilidade calibrada mínima: 65%;
- Wilson inferior mínimo: 55%;
- suporte mínimo do bloco: 50;
- máximo de cinco candidatos qualificados por ciclo;
- máximo real de três posições continua válido.

Sem amostra prospectiva e validação temporal suficientes, o estado é `INSUFFICIENT_SAMPLE`; nenhuma porcentagem é inventada.

## Persistência e consulta

O SQLite mantém:

- `nexus_reviews` — pareceres, Conselho, gates, motivos e hash;
- `paper_permits` — autorização PAPER de uso único;
- `paper_positions.nexus_permit_id` — vínculo da posição;
- `post_trade_audits` — auditoria posterior ao fechamento.

Consulta somente leitura:

```bash
python -m app.cli nexus-status --db data/paper.sqlite3
python -m app.cli nexus-status --db data/paper.sqlite3 --symbol BTCUSDT --limit 20
```

Hashes protegem a integridade do conteúdo local, mas não autenticam a origem de dados ou arquivos externos.

## Próximas integrações, ainda não implementadas

Antes de liberar `ENFORCE`, cada fonte externa precisa de contrato de dados, procedência, timestamp, validade, tratamento de indisponibilidade, testes e revisão de segurança. Isso inclui fundamentos, notícias, macro, on-chain e contexto amplo de mercado. A skill separada `token-launch-radar` serve para pesquisa pública e diligência de pré-lançamentos; ela não compra tokens nem libera automaticamente operações.
