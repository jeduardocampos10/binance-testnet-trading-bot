# Dashboard PAPER seguro

O dashboard mostra resultados e executa somente ações predefinidas. Ele não oferece terminal, não lê chaves Binance e não contém rotas de ordem.

## Funcionalidades

- períodos: dia, 7 dias, 30 dias e todo o histórico;
- patrimônio, retorno, P&L realizado/não realizado e drawdown;
- melhores e piores ativos, trades, vitórias, perdas e taxa de acerto;
- curva de patrimônio e resultados diários;
- tela moderna e responsiva para ordens PAPER, posições, risco, eventos e controles;
- ordens sempre identificadas como `FILLED_SIMULATED` e “não enviada à Binance”;
- saldo da Binance Spot Testnet via Bridge privado somente leitura, separado da contabilidade PAPER;
- posições, stops, Top 20, tendências, sinais, horários favoráveis e rejeições;
- configuração validada em modo somente leitura;
- ações autenticadas: `OBSERVATION`, `PAPER`, análise manual, pausar entradas, retomar entradas e exportar JSON.

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
- `/health` público expõe somente `status`; capacidades e APIs autenticadas mantêm `orders_enabled=false`;
- navegador nunca recebe o token do Bridge nem credenciais Binance;
- cliente do Bridge aceita apenas o hostname privado fixo/loopback, ignora proxies, rejeita redirects, limita resposta e falha fechado;
- indisponibilidade do Bridge degrada somente a tela de saldo; PAPER continua disponível;
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

A UI e as APIs autenticadas podem mostrar somente `llm_enabled` e o model ID, nunca a chave. O `/health` público retorna apenas `status`.

## Arquitetura unificada: PAPER + Bridge + Testnet

```text
Navegador autenticado
        |
        v
Dashboard / BFF (porta 8080, HTTPS no proxy)
  |-- SQLite PAPER em /data (ordens simuladas, posições, risco e eventos)
  `-- GET /balance + Bearer interno
             |
             v
     Binance Testnet Bridge (privado, sem porta pública)
             |
             `-- GET /api/v3/account na Binance Spot Testnet
```

A interface é única, mas as fronteiras permanecem explícitas:

- **PAPER:** simulação local; nenhuma ordem é enviada;
- **Testnet:** somente saldo consultado pelo Bridge;
- **produção:** desabilitada;
- **credenciais Binance:** existem exclusivamente no Bridge;
- **token interno:** compartilhado apenas entre dashboard e Bridge.

Novos endpoints autenticados e somente leitura:

- `GET /api/v1/capabilities`;
- `GET /api/v1/paper/orders`;
- `GET /api/v1/paper/positions`;
- `GET /api/v1/paper/risk`;
- `GET /api/v1/paper/events`;
- `GET /api/v1/testnet/balance`.

Não existe endpoint de ordem Binance ou Testnet. Uma futura execução Testnet exigirá um executor privado separado, revisão de segurança e autorização específica; não deve ser acrescentada ao Bridge de leitura.

## Compose unificado

`compose.unified.yml` conecta os dois serviços por uma rede interna dedicada. O Bridge também recebe uma rede de egress; **o Docker Compose, sozinho, não limita essa saída por hostname**. Uma allowlist externa de firewall/proxy deve restringi-la a `testnet.binance.vision:443` antes de qualquer deploy. O Bridge não possui `ports`, domínio, volume de dados ou acesso ao banco PAPER.

Antes de qualquer implantação autorizada:

1. revisar os testes e os códigos-fonte do dashboard e do projeto dedicado `binance-testnet-bridge`, publicando duas imagens próprias com proveniência e fixadas por digest;
2. definir `PAPER_DASHBOARD_IMAGE` e `BINANCE_TESTNET_BRIDGE_IMAGE` com referências `repository@sha256:...` distintas;
3. definir `DASHBOARD_PROXY_NETWORK` com a rede exata do proxy no ambiente não produtivo;
4. disponibilizar os quatro arquivos em `secrets/` (todos ignorados pelo Git): `dashboard_auth_token`, `bridge_auth_token`, `binance_testnet_api_key` e `binance_testnet_secret_key`;
5. executar `deploy/preflight-unified.py --secrets-dir ./secrets` sem expor valores;
6. confirmar que `bridge_auth_token` é o mesmo arquivo montado no dashboard e Bridge, sem reutilizar credencial Binance;
7. confirmar no host que somente o Bridge recebe as duas credenciais Binance;
8. aplicar e validar egress deny-by-default com allowlist para a Spot Testnet; o nome `bridge_egress` não é um controle de saída;
9. validar `/health`, depois `/api/v1/capabilities` e por fim uma única consulta autorizada a `/api/v1/testnet/balance`;
10. manter `orders_enabled=false` e não ativar agendamento.

O Compose é um artefato de implantação; sua existência não autoriza build, deploy, restart ou consulta autenticada à Binance.
