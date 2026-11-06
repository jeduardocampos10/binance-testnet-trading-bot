# Dashboard PAPER seguro

O dashboard mostra resultados e executa somente ações predefinidas. Ele não oferece terminal, não lê chaves Binance e não contém rotas de ordem.

## Funcionalidades

- períodos: dia, 7 dias, 30 dias e todo o histórico;
- patrimônio, retorno, P&L realizado/não realizado e drawdown;
- melhores e piores ativos, trades, vitórias, perdas e taxa de acerto;
- curva de patrimônio e resultados diários;
- posições, stops, Top 20, tendências, sinais, horários favoráveis e rejeições;
- configuração validada em modo somente leitura;
- ações autenticadas: `OBSERVATION`, `PAPER`, pausar entradas, retomar entradas e exportar JSON.

Alterar risco, estratégia ou universo exige nova configuração, versão e banco. Isso não pode ser feito pelo navegador enquanto um banco está ativo.

## Segurança

- Basic Auth: usuário fixo `admin`, senha lida exclusivamente de arquivo secreto;
- token mínimo de 24 caracteres;
- CSRF obrigatório em toda ação de estado;
- sem CORS, cache, frames ou MIME sniffing;
- uma única coleta em execução por vez;
- SQLite também serializa ciclos concorrentes;
- processo sem root (UID/GID 1000);
- somente endpoints públicos GET allowlisted;
- `orders_enabled=false` em saúde, APIs e relatórios;
- publique apenas por HTTPS no proxy do Coolify.

## Teste local dentro do container atual

Crie um segredo como `nanobot` sem mostrá-lo no terminal:

```bash
su -s /bin/bash nanobot -c '
set -eu
cd /home/nanobot/.nanobot/workspace/binance-testnet-trading-bot
umask 077
python -c "import secrets; print(secrets.token_urlsafe(32))" > data/dashboard-auth-token
chmod 600 data/dashboard-auth-token
'
```

Inicie vinculado somente ao localhost para validação interna:

```bash
su -s /bin/bash nanobot -c '
cd /home/nanobot/.nanobot/workspace/binance-testnet-trading-bot
python -m app.dashboard \
  --config config.json \
  --db data/paper-crypto-only-v05.sqlite3 \
  --auth-token-file data/dashboard-auth-token \
  --host 127.0.0.1 \
  --port 8080
'
```

O login é `admin`; a senha é o conteúdo do arquivo secreto. Não cole esse conteúdo em chat ou logs.

## Coolify — serviço separado recomendado

Não execute o painel como processo secundário improvisado no `nanobot-gateway`. Crie um serviço separado a partir de um repositório/contexto contendo apenas este projeto:

- Dockerfile: `Dockerfile.dashboard`;
- exemplo endurecido: `compose.dashboard.yml` (somente `expose`, propositalmente sem `ports`);
- porta interna: `8080`;
- healthcheck: `/health`;
- volume persistente dedicado: `/data`;
- secret file: `/run/secrets/dashboard_auth_token`, legível pelo UID 1000;
- domínio HTTPS exclusivo para o painel;
- nenhuma chave Binance e nenhum volume do Bridge;
- não montar todo o `cashmaxx_data` no serviço público.

O volume `/data` guarda `config.json`, o banco `paper-crypto-only-v05.sqlite3` e relatórios. Faça backup desse volume antes de atualizações.

### Política de rede

Permita saída HTTPS apenas quando a infraestrutura suportar allowlist:

- `data-api.binance.vision:443` — candles/tickers públicos;
- `testnet.binance.vision:443` — `exchangeInfo` público para interseção do universo.

Nenhuma porta do dashboard deve ser publicada diretamente no host. O Basic Auth usa uma credencial reutilizável e o aplicativo fala HTTP interno; portanto, conecte `8080` somente à rede privada do proxy Coolify e termine TLS/HTTPS no proxy. Ative HSTS no domínio. O arquivo `.dockerignore` impede que `config.json`, `data/`, tokens e bancos sejam enviados ao contexto de build.

## Agendamento

O botão PAPER é manual. Para uma rodada diária, use uma tarefa agendada do Coolify chamando o mesmo serviço/volume ou uma futura rota interna autenticada dedicada. Não agende antes de validar o primeiro relatório `v0.5-crypto-only` e o backup do banco.

## Domínio recomendado

Use `paper.artgenial.com.br`, terminado em HTTPS pelo proxy do Coolify. Não publique a porta 8080 diretamente e ative HSTS após confirmar que o domínio funciona somente por HTTPS.

## IA opcional para a aba “Análise manual”

Monte os arquivos regulares (não symlinks) `/run/secrets/openai_api_key` e `/run/secrets/openai_model`. No Compose, crie localmente `secrets/openai_api_key` e `secrets/openai_model` (ambos ignorados pelo Git), defina `OPENAI_API_KEY_FILE=./secrets/openai_api_key` e `OPENAI_MODEL_FILE=./secrets/openai_model`; o segundo arquivo contém somente o model ID habilitado. Sem essas variáveis, `/dev/null` é montado e a IA permanece desabilitada. Os dois arquivos são necessários em conjunto. Para executar sem IA, não passe `--openai-api-key-file` nem `--openai-model-file`; o dashboard permanece operacional e sem ordens.

Exemplo direto:

```sh
python -m app.dashboard --config /data/config.json --db /data/paper.sqlite3 \
  --auth-token-file /run/secrets/dashboard_auth_token \
  --openai-api-key-file /run/secrets/openai_api_key \
  --openai-model-file /run/secrets/openai_model
```

A UI e `/health` mostram apenas `llm_enabled` e o model ID, nunca a chave.
