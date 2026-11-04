create table public.categories (id int primary key, name text not null unique, description text default '');
create table public.stores (id int primary key, name text not null, category text not null, rating numeric(2,1) default 5.0);
create table public.products (
  id bigint generated always as identity primary key,
  store_id int references public.stores(id) on delete set null,
  category_id int references public.categories(id) on delete set null,
  name text not null, description text not null default '',
  price_cents int not null check (price_cents >= 0),
  image_url text default '', active boolean not null default true,
  created_at timestamptz not null default now());
create table public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  name text not null, role text not null default 'customer' check (role in ('customer','admin')));
create table public.orders (
  id bigint generated always as identity primary key,
  user_id uuid not null references public.profiles(id),
  delivery_address text not null,
  status text not null default 'received' check (status in ('received','preparing','out_for_delivery','completed','cancelled')),
  total_cents int not null, created_at timestamptz not null default now());
create table public.order_items (
  id bigint generated always as identity primary key,
  order_id bigint not null references public.orders(id) on delete cascade,
  product_id bigint not null references public.products(id),
  product_name_snapshot text not null, unit_price_cents int not null,
  quantity smallint not null check (quantity > 0));
create index on public.products (category_id);
create index on public.products (store_id);
create index on public.orders (user_id);
create index on public.order_items (order_id);
create index on public.order_items (product_id);

create function public.is_admin() returns boolean language sql stable security definer set search_path = '' as
$$ select exists (select 1 from public.profiles where id = (select auth.uid()) and role = 'admin') $$;

-- O primeiro usuário cadastrado vira administrador; os demais são clientes.
create function public.handle_new_user() returns trigger language plpgsql security definer set search_path = '' as $$
begin
  insert into public.profiles (id, name, role) values (new.id,
    coalesce(nullif(new.raw_user_meta_data->>'name',''), split_part(new.email,'@',1)),
    case when exists (select 1 from public.profiles where role = 'admin') then 'customer' else 'admin' end);
  return new;
end $$;
create trigger on_auth_user_created after insert on auth.users for each row execute function public.handle_new_user();
revoke execute on function public.handle_new_user() from public, anon, authenticated;

-- O preço sempre vem do banco; o cliente envia só produto e quantidade.
create function public.create_order(p_address text, p_items jsonb) returns bigint
language plpgsql security definer set search_path = public as $$
declare v_id bigint; v_total int;
begin
  if auth.uid() is null then raise exception 'Entre na sua conta'; end if;
  if length(trim(coalesce(p_address,''))) < 5 then raise exception 'Informe o endereço de entrega'; end if;
  insert into orders (user_id, delivery_address, total_cents) values (auth.uid(), trim(p_address), 0) returning id into v_id;
  insert into order_items (order_id, product_id, product_name_snapshot, unit_price_cents, quantity)
    select v_id, p.id, p.name, p.price_cents, least(sum(i.quantity), 99)::smallint
    from jsonb_to_recordset(p_items) as i(product_id bigint, quantity int)
    join products p on p.id = i.product_id and p.active
    where i.quantity > 0 group by p.id, p.name, p.price_cents;
  select coalesce(sum(unit_price_cents * quantity), 0) into v_total from order_items where order_id = v_id;
  if v_total = 0 then raise exception 'Nenhum produto válido no pedido'; end if;
  update orders set total_cents = v_total where id = v_id;
  return v_id;
end $$;

create function public.set_order_status(p_id bigint, p_status text) returns void
language plpgsql security definer set search_path = public as $$
declare o orders;
begin
  select * into o from orders where id = p_id;
  if not found or (o.user_id <> auth.uid() and not is_admin()) then raise exception 'Pedido não encontrado'; end if;
  if not is_admin() then
    if p_status <> 'cancelled' or o.status <> 'received' then raise exception 'Você só pode cancelar pedidos recebidos'; end if;
  elsif o.status in ('completed','cancelled') then raise exception 'Este pedido já foi finalizado'; end if;
  update orders set status = p_status where id = p_id;
end $$;
revoke execute on function public.create_order(text, jsonb) from public, anon;
revoke execute on function public.set_order_status(bigint, text) from public, anon;
grant execute on function public.create_order(text, jsonb) to authenticated;
grant execute on function public.set_order_status(bigint, text) to authenticated;

alter table public.categories enable row level security;
alter table public.stores enable row level security;
alter table public.products enable row level security;
alter table public.profiles enable row level security;
alter table public.orders enable row level security;
alter table public.order_items enable row level security;

create policy "categorias públicas" on public.categories for select using (true);
create policy "lojas públicas" on public.stores for select using (true);
create policy "produtos ativos públicos" on public.products for select using (active or public.is_admin());
create policy "admin gerencia categorias" on public.categories for all to authenticated using (public.is_admin()) with check (public.is_admin());
create policy "admin gerencia lojas" on public.stores for all to authenticated using (public.is_admin()) with check (public.is_admin());
create policy "admin gerencia produtos" on public.products for all to authenticated using (public.is_admin()) with check (public.is_admin());
create policy "perfil próprio ou admin" on public.profiles for select to authenticated using (id = (select auth.uid()) or public.is_admin());
create policy "pedidos próprios ou admin" on public.orders for select to authenticated using (user_id = (select auth.uid()) or public.is_admin());
create policy "itens próprios ou admin" on public.order_items for select to authenticated
  using (exists (select 1 from public.orders o where o.id = order_id and (o.user_id = (select auth.uid()) or public.is_admin())));
