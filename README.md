# iBag — plataforma de delivery

Site de delivery completo (cardápio, sacola, pedidos com acompanhamento, painel do administrador) com banco **PostgreSQL no Supabase**.

## Estrutura
- `index.html` — o site (HTML + CSS + JavaScript puro), conectado ao Supabase
- `supabase/migrations/` — esquema do banco (`001`) e cardápio de exemplo (`002`)
- `fastapi-sqlite/` — versão alternativa com API em Python (FastAPI) e SQLite
- `demo-offline/` — versão que simula a API no navegador (não precisa de banco)

## Como rodar
1. Abra o `index.html` no navegador (ou publique no GitHub Pages / Netlify / Vercel).
2. Cadastre-se: **o primeiro usuário cadastrado vira administrador**; os demais são clientes.
3. Para usar o seu próprio Supabase: crie um projeto, rode os dois SQL de `supabase/migrations/` no SQL Editor e troque `SUPABASE_URL` e `SUPABASE_KEY` no início do script do `index.html`.
4. Em *Authentication → Providers → Email*, desative **Confirm email** se quiser entrar logo após o cadastro, sem confirmar o e-mail.

## Segurança
- A chave usada no site é a chave **pública** do Supabase; a proteção está nas regras RLS do banco.
- Clientes só leem os próprios pedidos; só administradores alteram produtos.
- O total do pedido é calculado no banco (função `create_order`), nunca no navegador.
