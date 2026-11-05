# Especificação de segurança — versão 0.5 crypto-only

## Escopo

- Fonte de análise/PAPER: dados públicos não autenticados da Binance Spot principal no host somente de mercado `data-api.binance.vision`.
- Alvo de qualquer execução futura: exclusivamente Binance Spot Testnet; a versão atual não executa ordens.
- Universo: interseção dinâmica entre pares Testnet elegíveis e pares públicos Spot cotados em USDT.
- Exclusões crypto-only obrigatórias e incluídas no hash: stablecoins, ativos fiduciários, ouro/commodities tokenizadas, ações tokenizadas identificadas e tokens alavancados.
- Pares sem histórico, spread aceitável, liquidez ou mínimo Testnet compatível também são excluídos.
- Pré-filtro de até 60 pares por liquidez e ranking Top 20 recalculado diariamente.
- Timeframe principal: candle diário, fronteira UTC, somente candles fechados.
- Histórico mínimo: 90 candles válidos; coleta padrão: 120.
- Uma decisão por candle/símbolo/versão da estratégia.
- Cada banco PAPER fica vinculado ao hash integral da configuração; mudança de parâmetro exige banco novo e nova observação deliberada.
- Todo relatório declara fonte de dados, modo PAPER e alvo futuro Testnet; a fase atual não contém executor de ordens nem requisições autenticadas.
- Os únicos hosts permitidos são `data-api.binance.vision` para análise pública e `testnet.binance.vision` para elegibilidade pública do universo.

## Meta de resultado

`daily_goal_pct` aparece somente no relatório. Ela não participa do sinal, tamanho, frequência, recuperação de perdas ou ativação. Retornos não são garantidos. A meta padrão de 1% representa uma referência desejada, não uma obrigação diária.

## Ranking mensal

- retorno do mês corrente: peso 30%;
- retorno de 30 dias: peso 20%;
- liquidez de 24 horas: peso 20%;
- spread baixo: peso 15%;
- drawdown de 30 dias baixo: peso 15%.

Percentis reduzem o efeito de valores extremos. Estar no ranking não autoriza entrada. Retorno mensal e de 30 dias devem ser positivos, tendência e spread devem passar, e o mínimo de ordem deve caber no orçamento. O ranking não deve perseguir pumps nem contornar o sinal técnico.

## Indicadores e sinal

- Contexto longo: SMA60 e inclinação da SMA60 em cinco candles.
- Contexto médio: SMA30.
- Confirmação: cruzamento EMA7/EMA15.
- Filtros: RSI14 e volume/SMA20 de volume.
- Volatilidade: ATR14 para o plano de risco.
- Entrada: todas as condições descritas no README devem ser verdadeiras.
- Saída: cruzamento descendente EMA7/EMA15 ou fechamento abaixo da SMA30.
- Dados ausentes, duplicados, fora de ordem, não diários ou ainda abertos bloqueiam a decisão.

## Plano de risco baseline

- banca paper inicial: US$100;
- exposição máxima: 10% do patrimônio por posição;
- até três posições simultâneas na futura primeira versão executora;
- stop rígido confirmado: 5% abaixo do preço de entrada, e não 5% do patrimônio;
- trailing stop planejado: 3% abaixo do maior preço observado após alta;
- duas vezes o ATR14 não pode superar a distância de 5%; moedas mais voláteis são rejeitadas;
- perda teórica padrão por posição: 0,5% do patrimônio (10% de exposição × stop de 5%), antes de custos;
- alvo inicial: máximo entre 2R e retorno bruto necessário para a meta líquida após taxa e slippage, sem limitar a continuação via trailing;
- sem martingale, grid, averaging down ou aumento de risco após perda;
- cálculo decimal e arredondamento para baixo deverão respeitar `exchangeInfo` antes de qualquer ordem futura.

Limites implementados no simulador PAPER e obrigatórios para qualquer executor futuro:

- perda diária: 3% do patrimônio de 00:00 UTC, considerando perdas realizadas e não realizadas;
- ao alcançar o limite, bloquear novas entradas; posições não são liquidadas apenas pela trava e sua gestão protetiva continua;
- o restante do dia permanece em análise e paper simulation para o relatório;
- uma entrada por símbolo por dia;
- sem novas entradas quando dados, relógio, persistência ou reconciliação falharem;
- gestão de posição existente é independente da elegibilidade para novas entradas e nunca reabre um ativo fora do ranking elegível;
- stops e saídas protetivas continuam permitidos quando entradas estiverem bloqueadas.

Antes de qualquer executor Testnet ainda será necessário implementar alerta de drawdown de 3%, bloqueio operacional persistente em 5% e desbloqueio manual auditado.

## Estados planejados

- `OBSERVATION`: sinais e auditoria, sem ordens;
- `PAPER`: fills, taxas, slippage, gaps, stops e custos simulados, sem ordens;
- `TESTNET_ENABLED`: futuro executor Testnet;
- `PAUSED`: sem novas entradas;
- `LOCKED`: incidente/limite de risco, desbloqueio manual.

Nenhum estado muda automaticamente por completar 15 dias, gerar lucro ou alcançar uma quantidade de sinais. A frase escolhida pelo usuário, por exemplo `Robô pode trabalhar`, iniciará uma liberação futura, mas a aplicação deverá apresentar ambiente, parâmetros e hash da configuração antes da confirmação final. A liberação deve ser autenticada, auditada, reversível e explícita. Alterações materiais da estratégia exigem nova observação.

## Horário e fontes externas planejadas

Criptomoedas operam 24/7. Horários favoráveis são estimados por ativo com candles horários fechados, liquidez, spread atual, volatilidade controlada, amostra mínima e retorno ajustado ao risco. A estatística é contexto, não gatilho nem promessa. A apresentação usa `America/Sao_Paulo`; cálculos, candles e fronteira da trava diária usam UTC.

Dados externos poderão incluir CoinGecko para capitalização/volume, CryptoPanic para notícias estruturadas (requer token/plano) e Alternative.me para Fear & Greed. Notícias e sentimento não criarão compra sozinhos: serão usados como contexto ou veto de risco. Falha de fonte externa não será interpretada como sinal positivo.

## Dashboard operacional

- autenticação obrigatória por segredo em arquivo e transporte HTTPS no proxy;
- CSRF em ações de estado, sem CORS e sem execução de shell;
- uma ação de coleta por vez;
- setup mostra configuração validada, mas não altera parâmetros materiais do banco ativo;
- ações possíveis: `OBSERVATION`, `PAPER`, pausa, retomada e exportação;
- pausa bloqueia novas entradas, registra a simulação recusada e mantém saídas protetivas;
- nenhuma rota de conta, saldo, ordem, cancelamento ou Testnet habilitado.

## Requisitos antes de implementar ordens

- confirmar símbolo(s), capital de referência e limites;
- validar os resultados observados e um backtest fora da amostra;
- consultar e validar filtros atuais do símbolo;
- persistir intenção antes do envio;
- usar `clientOrderId` determinístico;
- reconciliar timeout de status desconhecido antes de qualquer retry;
- tratar fills parciais e impedir venda duplicada;
- proteger a posição com stop residente/OCO validado;
- kill switch e logs sem segredo ou URL assinada;
- segredo em arquivo montado, jamais no código, Git, argumentos ou chat.

## Limitações dos ambientes

O Testnet pode ser resetado e é usado apenas para elegibilidade do universo e como alvo de uma eventual execução futura. Candles, liquidez e spreads da análise vêm do mercado Spot público principal; fills, taxas e slippage continuam simulados e não demonstram rentabilidade futura. Dados reais de mercado não significam execução real. Uma eventual versão para dinheiro real será outro projeto e exigirá nova revisão.
