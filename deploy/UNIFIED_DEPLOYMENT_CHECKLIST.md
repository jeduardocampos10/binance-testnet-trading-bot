# Preparação de deploy — Dashboard PAPER + Bridge Testnet

Este documento prepara um deploy futuro, mas **não o autoriza**. Produção e `main` são proibidos. Commit, push, PR, publicação de imagem, deploy, restart, rollback e consulta autenticada à Binance exigem autorizações próprias.

## 1. Alvo obrigatório antes de qualquer escrita remota

Preencher e revalidar imediatamente antes da ação:

- Ambiente canônico não produtivo: `<dev | homologacao | saas-dev>`
- Plataforma/conta/time: `<valor confirmado>`
- Aplicação/recurso UUID: `<valor confirmado>`
- Domínio não produtivo: `<valor confirmado>`
- Rede externa do proxy: `<valor confirmado>`
- Repositório/branch: `jeduardocampos10/binance-testnet-trading-bot` / `feat/unified-paper-bridge-dashboard`
- Commit e tree revisados: `<SHA após commit autorizado>`
- Imagem Dashboard: `<registry/repository@sha256:digest>`
- Imagem Bridge: `<registry/repository@sha256:digest>`
- Identidade que executará o deploy: `<referência, sem credencial>`

Se qualquer campo estiver indefinido, limitar-se à descoberta read-only e não implantar.

## 2. Contrato de segurança

- Dashboard: PAPER-only; `orders_enabled=false`.
- Bridge: apenas `GET /health` e `GET /balance`.
- Nenhuma rota de criação/cancelamento de ordem.
- Credenciais Binance Testnet exclusivamente no Bridge.
- Dashboard recebe somente `dashboard_auth_token` e `bridge_auth_token`.
- Navegador não recebe token do Bridge nem credenciais Binance.
- Bridge sem domínio e sem `ports`.
- Dashboard e Bridge compartilham somente `bridge_private`.
- SQLite PAPER existe apenas no volume `dashboard_data`.
- Produção Binance não é configurável.
- Egress externo deny-by-default, permitindo somente DNS necessário e `testnet.binance.vision:443` para o Bridge.

## 3. Materiais necessários

Arquivos secretos fora do repositório e com permissões mínimas:

- `dashboard_auth_token`: aleatório, exclusivo, mínimo de 24 caracteres;
- `bridge_auth_token`: aleatório, exclusivo, mínimo de 32 caracteres;
- `binance_testnet_api_key`: credencial exclusiva da Spot Testnet;
- `binance_testnet_secret_key`: segredo correspondente;
- opcionais e atômicos: `openai_api_key` e `openai_model`.

Nunca exibir valores em terminal, logs, diff, ticket, screenshot ou relatório. Confirmar que os dois tokens internos não reutilizam credenciais Binance.

Confirmar por evidência sanitizada que a chave Binance:

- pertence exclusivamente à **Spot Testnet**;
- não é credencial de produção;
- possui o menor privilégio disponível;
- tem trading, saque e capacidades extras desabilitados quando a plataforma permitir;
- não é reutilizada em outro serviço.

Variáveis não secretas obrigatórias:

```text
PAPER_DASHBOARD_IMAGE=<imagem própria fixada por @sha256:...>
BINANCE_TESTNET_BRIDGE_IMAGE=<imagem própria fixada por @sha256:...>
DASHBOARD_PROXY_NETWORK=<nome exato da rede proxy do ambiente não produtivo>
```

## 4. Gates locais do candidato exato

Executar na árvore que será publicada:

```sh
python -m py_compile app/*.py tests/*.py
python -m unittest discover -s tests -p 'test_*.py'
git diff --check
git status --short
```

No projeto dedicado do Bridge:

```sh
python -m unittest discover -s tests -v
```

O contexto de build do Bridge deve excluir `binance-testnet-bridge-review.zip`, `*.zip` e todo artefato que não faça parte da imagem. Usar clone/contexto isolado e revisado, nunca o workspace Cashmaxx amplo.

Validar referências e arquivos secretos sem imprimir valores:

```sh
PAPER_DASHBOARD_IMAGE='registry/dashboard@sha256:<digest>' \
BINANCE_TESTNET_BRIDGE_IMAGE='registry/bridge@sha256:<digest>' \
DASHBOARD_PROXY_NETWORK='<rede-nao-produtiva>' \
python deploy/preflight-unified.py --secrets-dir ./secrets
```

Quando Docker estiver disponível, sem iniciar serviços:

```sh
PAPER_DASHBOARD_IMAGE='registry/dashboard@sha256:<digest>' \
BINANCE_TESTNET_BRIDGE_IMAGE='registry/bridge@sha256:<digest>' \
DASHBOARD_PROXY_NETWORK='<rede-nao-produtiva>' \
docker compose -f compose.unified.yml config --quiet
```

Depois, construir em isolamento e verificar digest, proveniência, usuário, healthcheck e ausência de segredo no histórico/camadas. O Compose de deploy usa somente imagens publicadas por digest; não compila no host de destino.

## 5. Sequência futura de implantação

1. Obter confirmação fresca para publicar as imagens do dashboard e Bridge, citando registry, repositórios e digests esperados.
2. Publicar ambas a partir dos códigos revisados; registrar digests e proveniência.
3. Repetir review, CSO, QA e verificação no commit/tree final do dashboard.
4. Obter confirmação fresca para deploy, citando ambiente, UUID/recurso, commit/tree, ambos os digests, impacto e rollback.
5. Criar snapshot verificável do volume PAPER, com hash, timestamp e tamanho, e testar restauração em ambiente isolado.
6. Confirmar compatibilidade da versão anterior com o snapshot/banco que será restaurado.
7. Aplicar secrets somente aos consumidores previstos.
8. Aplicar firewall/proxy de egress antes de fornecer credenciais ao Bridge.
9. Comprovar no proxy HTTPS: nenhuma exposição direta de `8080`, limites de conexão/taxa/header/body/timeout e logs sem `Authorization`.
10. Renderizar e validar o Compose com a versão usada pelo ambiente.
11. Implantar o Bridge privado e validar somente `/health`.
12. Implantar o dashboard e validar `/health`, TLS, autenticação e assets estáticos.
13. Validar `/api/v1/capabilities`: PAPER-only, produção desabilitada, ordens desabilitadas, Bridge read-only.
14. Com autorização separada, executar uma única consulta de saldo Testnet e confirmar que não há tentativa de ordem.
15. Inspecionar logs sanitizados, consumo de recursos e isolamento de rede/volumes.
16. Manter agendamento e ordens desabilitados.

## 6. Smoke pós-deploy

Sem revelar credenciais:

- `/health` retorna apenas `{"status":"ok"}`;
- HTTP redireciona para HTTPS e HSTS está presente;
- acesso sem Basic Auth recebe `401`;
- `/`, `/assets/app.css` e `/assets/app.js` carregam após autenticação;
- CSP, `X-Frame-Options: DENY`, `nosniff`, `no-referrer` e `no-store` estão presentes;
- `/api/v1/capabilities` informa `paper_only=true` e `orders_enabled=false`;
- ordens PAPER mostram `FILLED_SIMULATED` e `sent_to_binance=false`;
- saldo Testnet não altera patrimônio PAPER;
- indisponibilidade do Bridge não derruba PAPER;
- `/api/v1/testnet/order` e métodos não allowlisted permanecem inexistentes/rejeitados;
- Bridge não possui rota pública, acesso ao volume PAPER ou credencial de produção;
- logs não contêm `Authorization`, chaves, assinatura, resposta bruta da conta ou PII.

## 7. Rollback

Rollback preparado, mas não automaticamente autorizado. Sua execução ou qualquer restart exige confirmação humana fresca vinculada ao ambiente, recurso, commit/tree, ambos os digests, impacto e janela, salvo procedimento emergencial previamente aprovado:

1. preservar snapshot anterior do volume `dashboard_data`, com hash, timestamp e tamanho;
2. testar a restauração do snapshot em ambiente isolado e verificar compatibilidade com a versão anterior;
3. preservar digest anterior do dashboard;
4. preservar digest anterior do Bridge;
5. em falha apenas do saldo, desconectar/desabilitar o Bridge e manter PAPER disponível;
6. em regressão do dashboard, restaurar o digest anterior e o snapshot compatível;
7. abortar o rollback se digest, snapshot, compatibilidade ou autorização divergirem do recibo;
8. considerar o rollback concluído somente após health, autenticação, PAPER, isolamento e logs serem revalidados;
9. se houver suspeita de exposição, interromper novas escritas, isolar o Bridge e executar o fluxo autorizado de revogação/rotação — nunca imprimir o segredo.

## 8. Evidência mínima a registrar

- ambiente, recurso e domínio não produtivos;
- commit SHA e tree SHA;
- digests das duas imagens e imagens-base;
- resultados de review, CSO, QA e testes;
- confirmação humana específica por ação;
- timestamp de deploy e operador;
- snapshot restaurável e mecanismo de rollback testado;
- respostas sanitizadas de health/capabilities;
- prova de isolamento de secrets, redes e volumes;
- prova positiva e negativa do egress;
- resultado da única consulta Testnet, sem saldos sensíveis no relatório.
