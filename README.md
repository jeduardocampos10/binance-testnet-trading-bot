# Binance Spot Testnet — robô acompanhado

Fase acompanhada de um robô **long-only** para a Binance Spot Testnet. Esta versão opera somente em análise pública e **PAPER**:

- descobre pares elegíveis no Spot Testnet e os intersecta com o mercado público Spot em USDT;
- elimina moedas estáveis, tokens alavancados, spreads altos e pares cujo mínimo Testnet não cabe na banca;
- pré-seleciona até 60 pares por liquidez pública e cria um ranking Top 20 mensal;
- baixa candles públicos reais do host fixo e somente de mercado `data-api.binance.vision`, descartando o candle aberto;
- calcula contexto de 60/30 dias e cruzamento de 7/15 dias;
- produz um plano hipotético de risco para cada ativo elegível;
- estima até três horários favoráveis por ativo com candles horários fechados, exibidos em `America/Sao_Paulo`;
- simula em PAPER entradas, taxas, slippage, stop rígido, trailing stop e saídas;
- aplica trava diária de 3% sem liquidar automaticamente posições que continuam protegidas;
- após a trava, registra entradas candidatas apenas como simulações contrafactuais;
- grava ranking, decisões, posições, eventos e relatórios auditáveis em SQLite;
- oferece dashboard autenticado com análises por dia/semana/mês, tendências e ações PAPER predefinidas.

**Não existe código autenticado nem código para criar, cancelar ou consultar ordens.** O uso de dados públicos da Binance Spot principal não autoriza nem possibilita negociação real. A versão atual não consegue negociar nem mesmo se uma chave estiver disponível. Não use chaves da Binance com esta fase. O Bridge somente leitura permanece separado.

## Princípios

- A meta de 1% é informativa e não é garantia.
- O robô nunca abre uma operação para cumprir meta diária.
- Perder menos e ficar fora do mercado é preferível a forçar um sinal.
- Dados públicos Spot reais somente para análise; qualquer futura execução permanece exclusivamente no Spot Testnet, sem margem, futuros, alavancagem ou short.
- O Bridge `binance-testnet-bridge` permanece separado e somente leitura.

## Ranking e estratégia baseline

O Top 20 não é uma lista simples das maiores altas. A versão `monthly-top20-trend-cross-v0.5-crypto-only` exclui por configuração auditável stablecoins, ativos fiduciários, ouro/commodities tokenizadas, ações tokenizadas identificadas e tokens alavancados. O score combina retorno do mês (30%), retorno de 30 dias (20%), liquidez (20%), spread baixo (15%) e drawdown baixo (15%). Uma moeda ranqueada somente fica elegível se o retorno mensal e de 30 dias forem positivos, a tendência 30/60 estiver favorável, o spread estiver dentro do limite e o valor mínimo da ordem couber na posição de US$10.

Depois do ranking, um candidato de entrada exige simultaneamente:

1. fechamento acima da SMA60;
2. SMA30 acima da SMA60;
3. SMA60 maior que há cinco candles;
4. EMA7 cruzando de baixo para cima a EMA15 no último candle fechado;
5. RSI14 entre 50 e 68;
6. volume do dia pelo menos 80% da média de 20 dias.

Um candidato de saída aparece quando EMA7 cruza abaixo da EMA15 ou o fechamento fica abaixo da SMA30. Em `observe`, o sinal é apenas registrado; em `paper`, ele fecha somente uma posição simulada.

A banca PAPER inicial é US$100. Cada posição usa no máximo 10% do patrimônio, com até três posições. Os 5% representam a **distância do stop no preço**, não 5% da banca: uma posição de US$10 arrisca teoricamente cerca de US$0,50 antes de custos e gaps. Duas vezes o ATR não pode exceder essa distância, ou a moeda é rejeitada por volatilidade. Depois que o preço sobe, o trailing stop fica 3% abaixo do maior preço observado. O alvo inicial é pelo menos 2R, mas o trailing pode capturar movimentos maiores. A meta diária de 1% nunca limita ganhos nem força uma entrada.

A perda diária é medida desde o patrimônio de abertura do dia UTC. Ao atingir 3%, novas entradas ficam bloqueadas até o próximo dia UTC. Posições abertas **não são liquidadas somente por causa da trava**: stops e sinais protetivos continuam funcionando. Relatórios são apresentados em `America/Sao_Paulo`; candles, fronteiras diárias e controles de risco permanecem em UTC.

Como esta fase processa candles diários fechados, stops PAPER não são ordens residentes intradiárias. O simulador usa OHLC diário, considera gap na abertura e slippage configurado, mas não conhece a sequência intradiária exata. Resultados PAPER não demonstram rentabilidade futura.

## Executar testes

```bash
cd binance-testnet-trading-bot
python -m unittest discover -s tests -v
```

Os testes não acessam a rede.

## Executar observação ou PAPER

Copie a configuração e ajuste apenas valores não secretos:

```bash
cp config.example.json config.json
python -m app.cli observe --config config.json --db data/observer.sqlite3
```

Executar uma rodada PAPER persistente (ainda somente com endpoints públicos):

```bash
python -m app.cli paper --config config.json --db data/paper-crypto-only-v05.sqlite3
```

Consultar o último relatório sem acessar a Binance:

```bash
python -m app.cli status --db data/paper-crypto-only-v05.sqlite3
```

`observe` registra análise sem alterar carteira. `paper` usa dados públicos Spot reais, mas altera apenas a carteira simulada local. Os relatórios declaram `market_data_source=BINANCE_PUBLIC_PRODUCTION`, `execution_mode=PAPER` e `future_execution_target=BINANCE_SPOT_TESTNET`. Use sempre o mesmo banco novo para preservar posições, trava diária e auditoria. O banco PAPER fica vinculado ao hash integral da configuração; qualquer alteração exige outro banco e nova observação deliberada. Nenhum comando cria tarefa recorrente automaticamente.

## Dashboard

O dashboard inclui visão geral, resultados por período, curva de patrimônio, melhores/piores ativos, drawdown, posições, tendências, ranking, rejeições e um menu separado de setup/ações. Somente ações predefinidas são aceitas: observar, executar PAPER, pausar/retomar entradas e exportar relatórios. Parâmetros materiais são somente leitura e exigem novo banco/versão.

Leia [DASHBOARD.md](DASHBOARD.md) antes de testar ou publicar. Não exponha a porta sem HTTPS, arquivo secreto e autenticação verificada.

## Próximas fases

1. Acompanhar e revisar sinais e fills em `OBSERVATION`/`PAPER` por até 15 dias.
2. Congelar regras e executar backtest sem look-ahead, com custos.
3. Implementar reconciliação e testes de falhas de ordens em simulador.
4. Somente após autorização explícita, criar executor Testnet separado e protegido.
5. Nunca promover automaticamente por tempo ou rentabilidade.

Leia também [SPEC.md](SPEC.md) e [DASHBOARD.md](DASHBOARD.md).

## Análise manual Top 10 (consultiva)

O dashboard oferece uma análise manual de um ativo que pertença ao Top 10 de uma observação nova. O relatório combina ranking, decisão e plano de risco determinísticos, barra fechada, perfil de sessão e verificações de integridade. Quando configurada, a OpenAI Responses API fornece apenas uma avaliação estruturada consultiva; ela não altera símbolo, quantidade, stop ou alvo e o projeto não expõe qualquer capacidade de ordem. Sem os dois secrets OpenAI, o dashboard continua funcionando e a avaliação fica `INCONCLUSIVE`.
