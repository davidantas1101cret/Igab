import os
import threading
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Literal

import bcrypt
import jwt
from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, ConfigDict, EmailStr, Field, computed_field
from sqlalchemy import (
    BigInteger, Boolean, DateTime, Enum, ForeignKey, Integer, Numeric,
    SmallInteger, String, create_engine, event, func, or_, select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, Session, mapped_column, relationship,
    selectinload, sessionmaker,
)

# =====================================================================
# CONFIG  (nada para configurar: o banco é um arquivo SQLite criado sozinho)
# =====================================================================
BASE_DIR = Path(__file__).resolve().parent
DATABASE_URL = f"sqlite:///{BASE_DIR / 'ibag.db'}"
JWT_SECRET = "troque-por-uma-frase-longa-e-secreta"
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = 120
ADMIN_EMAIL = "admin@ibag.com"
ADMIN_SENHA = "admin123"

# =====================================================================
# BANCO DE DADOS
# =====================================================================
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})


@event.listens_for(engine, "connect")
def _sqlite_fk(dbapi_conn, _):
    dbapi_conn.execute("PRAGMA foreign_keys=ON")  # faz valer ON DELETE / chaves estrangeiras


BigInt = BigInteger().with_variant(Integer, "sqlite")  # autoincrement funciona no SQLite
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# =====================================================================
# MODELS
# =====================================================================
ORDER_STATUSES = ("received", "preparing", "out_for_delivery", "completed", "cancelled")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(Enum("customer", "admin"), default="customer")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Store(Base):
    __tablename__ = "stores"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(60))
    rating: Mapped[float | None] = mapped_column(Numeric(2, 1, asdecimal=False), default=5.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)
    description: Mapped[str | None] = mapped_column(String(255), default="")


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=True)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"))
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(String(500), default="")
    price_cents: Mapped[int] = mapped_column(Integer)
    image_url: Mapped[str | None] = mapped_column(String(255), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    delivery_address: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(Enum(*ORDER_STATUSES), default="received")
    total_cents: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    product_name_snapshot: Mapped[str] = mapped_column(String(160))
    unit_price_cents: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(SmallInteger)

    order: Mapped[Order] = relationship(back_populates="items")


# =====================================================================
# SCHEMAS (Pydantic)
# =====================================================================
class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


def brl(cents: int) -> str:
    s = f"{cents / 100:,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


class UserCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=6, max_length=72)


class UserOut(ORM):
    id: int
    name: str
    email: EmailStr
    role: Literal["customer", "admin"]
    created_at: datetime


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=255)


class CategoryOut(ORM, CategoryIn):
    id: int


class StoreIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    category: str = Field(min_length=1, max_length=60)
    rating: float = Field(default=5.0, ge=0, le=5)


class StoreOut(ORM, StoreIn):
    id: int


class ProductIn(BaseModel):
    store_id: int | None = None
    category_id: int | None = None
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=500)
    price_cents: int = Field(ge=0)
    image_url: str = Field(default="", max_length=255)
    active: bool = True


class ProductUpdate(BaseModel):
    store_id: int | None = None
    category_id: int | None = None
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=500)
    price_cents: int | None = Field(default=None, ge=0)
    image_url: str | None = Field(default=None, max_length=255)
    active: bool | None = None


class ProductOut(ORM):
    id: int
    store_id: int | None
    category_id: int | None
    name: str
    description: str
    price_cents: int
    image_url: str | None
    active: bool

    @computed_field
    @property
    def price_display(self) -> str:
        return brl(self.price_cents)


class OrderItemIn(BaseModel):
    product_id: int
    quantity: int = Field(ge=1, le=99)


class OrderCreate(BaseModel):
    delivery_address: str = Field(min_length=5, max_length=500)
    items: list[OrderItemIn] = Field(min_length=1)


class OrderItemOut(ORM):
    product_id: int
    product_name_snapshot: str
    unit_price_cents: int
    quantity: int


class OrderOut(ORM):
    id: int
    user_id: int
    delivery_address: str
    status: str
    total_cents: int
    created_at: datetime
    items: list[OrderItemOut]

    @computed_field
    @property
    def total_display(self) -> str:
        return brl(self.total_cents)


class OrderStatusUpdate(BaseModel):
    status: Literal["received", "preparing", "out_for_delivery", "completed", "cancelled"]


# =====================================================================
# SEGURANÇA (JWT + bcrypt)
# =====================================================================
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def create_access_token(user: User) -> str:
    payload = {
        "sub": str(user.id),
        "role": user.role,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def get_current_user(
    token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> User:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Token inválido ou expirado",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user_id = int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise credentials_error

    user = db.get(User, user_id)
    if not user:
        raise credentials_error
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Acesso restrito a administradores")
    return user


# =====================================================================
# APP
# =====================================================================
CATEGORIAS = [
    (1, "Exclusivos iBag", "Produtos oficiais e ecológicos da marca iBag"),
    (2, "Hambúrgueres", "Burgers artesanais, suculentos e combos"),
    (3, "Pizzas", "Pizzas salgadas e doces assadas no forno a lenha"),
    (4, "Comida Japonesa", "Sushis, temakis, sashimis e combos orientais"),
    (5, "Bebidas", "Refrigerantes, sucos naturais e chás"),
    (6, "Sobremesas", "Doces, bolos e sorvetes para adoçar o seu dia"),
    (7, "Massas", "Massas frescas, lasanhas e molhos artesanais"),
    (8, "Saudáveis", "Saladas, bowls e opções leves"),
    (9, "Mexicana", "Tacos, burritos e nachos"),
    (10, "Padaria & Café", "Pães, cafés e lanches rápidos"),
]
LOJAS = [
    (1, "Loja Oficial iBag", "Acessórios", 5.0),
    (2, "iBag Burger House", "Hambúrgueres", 4.8),
    (3, "Pizzeria Bella Napoli", "Pizzas", 4.7),
    (4, "Sakura Sushi Bar", "Comida Japonesa", 4.9),
    (5, "Doceria Doce Sonho", "Sobremesas", 4.6),
    (6, "Cantina Nonna Lucia", "Massas", 4.8),
    (7, "Verde Bowl", "Saudáveis", 4.7),
    (8, "El Taco Loco", "Mexicana", 4.5),
    (9, "Padaria Pão Quente", "Padaria & Café", 4.6),
]
PRODUTOS = [  # (loja, categoria, nome, descrição, preço em centavos)
    (1, 1, "Sacola Térmica iBag Pro", "Sacola ecológica e reutilizável iBag. Mantém o seu pedido quente ou gelado por mais tempo.", 1990),
    (1, 1, "Mochila Entregador iBag", "Mochila reforçada impermeável com isolamento térmico de alta performance.", 12900),
    (2, 2, "Hambúrguer iBag Smash", "Pão brioche, 2 smash burgers de 90g, queijo prato e molho da casa.", 3290),
    (2, 2, "Cheese Bacon", "Blend 160g, cheddar, bacon crocante e cebola caramelizada.", 3690),
    (2, 2, "Burger Veggie", "Hambúrguer de grão-de-bico, rúcula, tomate e maionese verde.", 3490),
    (3, 3, "Pizza Margherita", "Molho de tomate, muçarela de búfala e manjericão fresco.", 4590),
    (3, 3, "Pizza Calabresa", "Calabresa artesanal, cebola roxa e azeitonas.", 4290),
    (3, 3, "Pizza Quatro Queijos", "Muçarela, gorgonzola, parmesão e provolone.", 4890),
    (4, 4, "Combo Sushi 20 peças", "Salmão, atum e kani, com shoyu e gengibre.", 5490),
    (4, 4, "Temaki de Salmão", "Salmão fresco, cream cheese e cebolinha.", 2790),
    (4, 4, "Sashimi Misto", "12 fatias de salmão, atum e peixe branco.", 4290),
    (2, 5, "Suco Natural de Laranja", "Espremido na hora, 500ml.", 1090),
    (2, 5, "Refrigerante Lata", "Lata 350ml gelada. Cola, guaraná ou limão.", 690),
    (4, 5, "Chá Gelado de Pêssego", "Chá preto com pêssego, 400ml.", 890),
    (5, 6, "Brigadeiro Gourmet", "Brigadeiro de chocolate belga, caixa com 6.", 2290),
    (5, 6, "Petit Gâteau", "Bolo quente de chocolate com sorvete de creme.", 2590),
    (5, 6, "Torta de Limão", "Fatia generosa com merengue maçaricado.", 1890),
    (2, 2, "Duplo Cheddar Melt", "Dois blends de 120g, muito cheddar cremoso e cebola crispy.", 4190),
    (2, 2, "Burger BBQ Crocante", "Blend 160g, bacon, anéis de cebola e molho barbecue defumado.", 3890),
    (2, 2, "Batata Frita Crocante", "Porção generosa de batata frita sequinha com sal e páprica.", 1890),
    (2, 2, "Onion Rings", "Anéis de cebola empanados, com molho especial da casa.", 2190),
    (3, 3, "Pizza Frango com Catupiry", "Frango desfiado, catupiry original e milho.", 4490),
    (3, 3, "Pizza Portuguesa", "Presunto, ovos, cebola, ervilha e azeitona.", 4390),
    (3, 3, "Pizza Pepperoni", "Pepperoni fatiado, muçarela e orégano.", 4690),
    (3, 3, "Pizza de Chocolate com Morango", "Chocolate ao leite derretido e morangos frescos.", 3990),
    (4, 4, "Hot Roll Salmão (10 peças)", "Salmão empanado e frito, com cream cheese e molho tarê.", 3290),
    (4, 4, "Uramaki Filadélfia (8 peças)", "Salmão, cream cheese e cebolinha.", 2890),
    (4, 4, "Yakisoba de Frango", "Macarrão oriental salteado com legumes e frango.", 3490),
    (4, 4, "Gyoza (6 unidades)", "Pastéis japoneses de carne suína grelhados.", 2490),
    (2, 5, "Suco de Maracujá 500ml", "Natural, feito na hora. Pode pedir sem açúcar.", 1190),
    (2, 5, "Limonada Suíça", "Limão batido com leite condensado, 500ml.", 1290),
    (2, 5, "Cerveja Artesanal IPA 355ml", "Lúpulo cítrico e amargor equilibrado. Gelada.", 1890),
    (4, 5, "Água Mineral 500ml", "Sem gás ou com gás.", 490),
    (9, 5, "Café Expresso", "Café especial de grãos selecionados.", 590),
    (9, 5, "Cappuccino Cremoso", "Café com leite vaporizado e canela.", 1090),
    (5, 6, "Cheesecake de Frutas Vermelhas", "Base crocante e calda de frutas vermelhas.", 2490),
    (5, 6, "Sorvete Artesanal 2 Bolas", "Sabores do dia, com calda e farofa de castanha.", 1790),
    (5, 6, "Churros Recheados (6 un.)", "Recheio de doce de leite e açúcar com canela.", 2190),
    (5, 6, "Pudim de Leite Condensado", "Pudim cremoso com calda de caramelo.", 1490),
    (5, 6, "Açaí 500ml com Frutas", "Açaí cremoso com banana, morango e granola.", 2690),
    (6, 7, "Lasanha à Bolonhesa", "Camadas de massa fresca, molho bolonhesa e queijo gratinado.", 4290),
    (6, 7, "Espaguete ao Pomodoro", "Espaguete al dente com molho de tomate fresco e manjericão.", 3490),
    (6, 7, "Fettuccine Alfredo", "Fettuccine em molho cremoso de queijo parmesão.", 3990),
    (6, 7, "Nhoque ao Sugo", "Nhoque macio com molho de tomate e parmesão.", 3690),
    (7, 8, "Salada Caesar com Frango", "Alface americana, frango grelhado, parmesão e croutons.", 3290),
    (7, 8, "Bowl de Salmão", "Arroz, salmão grelhado, abacate, pepino e gergelim.", 3990),
    (7, 8, "Wrap Integral de Frango", "Wrap integral com frango, folhas e molho de iogurte.", 2790),
    (7, 8, "Sanduíche Natural", "Pão integral, peito de peru, queijo branco e cenoura.", 2290),
    (7, 5, "Suco Detox Verde", "Couve, limão, gengibre e maçã.", 1390),
    (8, 9, "Tacos de Carne (3 un.)", "Carne temperada, guacamole, pico de gallo e coentro.", 3290),
    (8, 9, "Burrito de Frango", "Tortilha, frango, feijão, arroz e queijo.", 3490),
    (8, 9, "Nachos com Queijo", "Tortilhas crocantes com cheddar e jalapeño.", 2790),
    (8, 9, "Quesadilla de Queijo", "Tortilha grelhada recheada de queijo derretido.", 2590),
    (9, 10, "Pão de Queijo (6 un.)", "Quentinho, crocante por fora e macio por dentro.", 1290),
    (9, 10, "Croissant de Chocolate", "Massa folhada amanteigada com recheio de chocolate.", 1490),
    (9, 10, "Misto Quente", "Pão de forma, presunto e queijo na chapa.", 1590),
    (9, 10, "Bolo de Cenoura com Chocolate", "Fatia generosa com cobertura de chocolate.", 1390),
]


def seed(db: Session):
    """Preenche o banco na 1ª vez e, nas seguintes, acrescenta o que estiver faltando (por nome)."""
    primeira = not db.scalar(select(func.count(Category.id)))
    ids_cat = set(db.scalars(select(Category.id)))
    db.add_all(Category(id=i, name=n, description=d) for i, n, d in CATEGORIAS if i not in ids_cat)
    ids_loja = set(db.scalars(select(Store.id)))
    db.add_all(Store(id=i, name=n, category=c, rating=r) for i, n, c, r in LOJAS if i not in ids_loja)
    db.flush()
    nomes = set(db.scalars(select(Product.name)))
    db.add_all(
        Product(store_id=s_, category_id=c, name=n, description=d, price_cents=p)
        for s_, c, n, d, p in PRODUTOS if n not in nomes
    )
    if primeira:
        db.add(User(name="Administrador", email=ADMIN_EMAIL,
                    password_hash=hash_password(ADMIN_SENHA), role="admin"))
    db.commit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(engine)  # cria as tabelas se não existirem
    with SessionLocal() as db:
        seed(db)
    yield


app = FastAPI(title="iBag API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_or_404(db: Session, model, obj_id: int, label: str):
    obj = db.get(model, obj_id)
    if not obj:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{label} não encontrado(a)")
    return obj


# ----------------------------- O SITE --------------------------------
@app.get("/", include_in_schema=False)
def home():
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"))


@app.get("/health", tags=["Sistema"])
def health(db: Session = Depends(get_db)):
    db.execute(select(1))
    return {"status": "ok"}


# ------------------------------- AUTH --------------------------------
@app.post("/auth/register", response_model=UserOut, status_code=201, tags=["Auth"])
def register(data: UserCreate, db: Session = Depends(get_db)):
    email = data.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "E-mail já cadastrado")
    user = User(
        name=data.name, email=email, password_hash=hash_password(data.password), role="customer"
    )
    db.add(user)
    db.commit()
    return user


@app.post("/auth/login", response_model=Token, tags=["Auth"])
def login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    # No campo "username" do formulário vai o e-mail
    user = db.scalar(select(User).where(User.email == form.username.lower()))
    if not user or not verify_password(form.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "E-mail ou senha incorretos")
    return {"access_token": create_access_token(user), "token_type": "bearer", "user": user}


@app.get("/auth/me", response_model=UserOut, tags=["Auth"])
def me(user: User = Depends(get_current_user)):
    return user


# ----------------------------- CATEGORIAS ----------------------------
@app.get("/categories", response_model=list[CategoryOut], tags=["Categorias"])
def list_categories(db: Session = Depends(get_db)):
    return db.scalars(select(Category).order_by(Category.id)).all()


@app.post("/categories", response_model=CategoryOut, status_code=201,
          tags=["Categorias"], dependencies=[Depends(require_admin)])
def create_category(data: CategoryIn, db: Session = Depends(get_db)):
    cat = Category(**data.model_dump())
    db.add(cat)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Já existe uma categoria com esse nome")
    return cat


@app.put("/categories/{category_id}", response_model=CategoryOut,
         tags=["Categorias"], dependencies=[Depends(require_admin)])
def update_category(category_id: int, data: CategoryIn, db: Session = Depends(get_db)):
    cat = get_or_404(db, Category, category_id, "Categoria")
    for k, v in data.model_dump().items():
        setattr(cat, k, v)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Já existe uma categoria com esse nome")
    return cat


@app.delete("/categories/{category_id}", status_code=204,
            tags=["Categorias"], dependencies=[Depends(require_admin)])
def delete_category(category_id: int, db: Session = Depends(get_db)):
    db.delete(get_or_404(db, Category, category_id, "Categoria"))
    db.commit()
    return Response(status_code=204)


# ------------------------------- LOJAS -------------------------------
@app.get("/stores", response_model=list[StoreOut], tags=["Lojas"])
def list_stores(category: str | None = None, db: Session = Depends(get_db)):
    stmt = select(Store).order_by(Store.rating.desc(), Store.name)
    if category:
        stmt = stmt.where(Store.category == category)
    return db.scalars(stmt).all()


@app.get("/stores/{store_id}", response_model=StoreOut, tags=["Lojas"])
def get_store(store_id: int, db: Session = Depends(get_db)):
    return get_or_404(db, Store, store_id, "Loja")


@app.post("/stores", response_model=StoreOut, status_code=201,
          tags=["Lojas"], dependencies=[Depends(require_admin)])
def create_store(data: StoreIn, db: Session = Depends(get_db)):
    store = Store(**data.model_dump())
    db.add(store)
    db.commit()
    return store


@app.put("/stores/{store_id}", response_model=StoreOut,
         tags=["Lojas"], dependencies=[Depends(require_admin)])
def update_store(store_id: int, data: StoreIn, db: Session = Depends(get_db)):
    store = get_or_404(db, Store, store_id, "Loja")
    for k, v in data.model_dump().items():
        setattr(store, k, v)
    db.commit()
    return store


@app.delete("/stores/{store_id}", status_code=204,
            tags=["Lojas"], dependencies=[Depends(require_admin)])
def delete_store(store_id: int, db: Session = Depends(get_db)):
    db.delete(get_or_404(db, Store, store_id, "Loja"))  # produtos ficam com store_id NULL
    db.commit()
    return Response(status_code=204)


# ------------------------------ PRODUTOS -----------------------------
@app.get("/products", response_model=list[ProductOut], tags=["Produtos"])
def list_products(
    store_id: int | None = None,
    category_id: int | None = None,
    q: str | None = Query(None, description="Busca por nome/descrição"),
    include_inactive: bool = False,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    stmt = select(Product).order_by(Product.id)
    if not include_inactive:
        stmt = stmt.where(Product.active.is_(True))
    if store_id:
        stmt = stmt.where(Product.store_id == store_id)
    if category_id:
        stmt = stmt.where(Product.category_id == category_id)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Product.name.like(like), Product.description.like(like)))
    return db.scalars(stmt.limit(limit).offset(offset)).all()


@app.get("/products/{product_id}", response_model=ProductOut, tags=["Produtos"])
def get_product(product_id: int, db: Session = Depends(get_db)):
    return get_or_404(db, Product, product_id, "Produto")


def _validate_refs(db: Session, store_id, category_id):
    if store_id is not None and not db.get(Store, store_id):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "store_id inexistente")
    if category_id is not None and not db.get(Category, category_id):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "category_id inexistente")


@app.post("/products", response_model=ProductOut, status_code=201,
          tags=["Produtos"], dependencies=[Depends(require_admin)])
def create_product(data: ProductIn, db: Session = Depends(get_db)):
    _validate_refs(db, data.store_id, data.category_id)
    product = Product(**data.model_dump())
    db.add(product)
    db.commit()
    return product


@app.patch("/products/{product_id}", response_model=ProductOut,
           tags=["Produtos"], dependencies=[Depends(require_admin)])
def update_product(product_id: int, data: ProductUpdate, db: Session = Depends(get_db)):
    product = get_or_404(db, Product, product_id, "Produto")
    changes = data.model_dump(exclude_unset=True)
    _validate_refs(db, changes.get("store_id"), changes.get("category_id"))
    for k, v in changes.items():
        setattr(product, k, v)
    db.commit()
    return product


@app.delete("/products/{product_id}", status_code=204,
            tags=["Produtos"], dependencies=[Depends(require_admin)])
def delete_product(product_id: int, db: Session = Depends(get_db)):
    """Exclusão lógica: order_items referencia products sem CASCADE,
    então apenas desativamos o produto para preservar o histórico de pedidos."""
    product = get_or_404(db, Product, product_id, "Produto")
    product.active = False
    db.commit()
    return Response(status_code=204)


# ------------------------------ PEDIDOS ------------------------------
def _order_query():
    return select(Order).options(selectinload(Order.items))


@app.post("/orders", response_model=OrderOut, status_code=201, tags=["Pedidos"])
def create_order(
    data: OrderCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Agrupa itens repetidos do mesmo produto
    quantities: dict[int, int] = {}
    for item in data.items:
        quantities[item.product_id] = quantities.get(item.product_id, 0) + item.quantity

    products = db.scalars(select(Product).where(Product.id.in_(quantities))).all()
    by_id = {p.id: p for p in products}

    for pid in quantities:
        p = by_id.get(pid)
        if not p or not p.active:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, f"Produto {pid} indisponível ou inexistente"
            )
        if quantities[pid] > 99:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "Quantidade máxima por produto: 99"
            )

    # O preço SEMPRE vem do banco, nunca do cliente
    order = Order(user_id=user.id, delivery_address=data.delivery_address, total_cents=0)
    total = 0
    for pid, qty in quantities.items():
        p = by_id[pid]
        total += p.price_cents * qty
        order.items.append(
            OrderItem(
                product_id=p.id,
                product_name_snapshot=p.name,
                unit_price_cents=p.price_cents,
                quantity=qty,
            )
        )
    order.total_cents = total

    db.add(order)
    db.commit()
    return order


@app.get("/orders", response_model=list[OrderOut], tags=["Pedidos"])
def list_orders(
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Cliente vê só seus pedidos; admin vê todos."""
    stmt = _order_query().order_by(Order.id.desc())
    if user.role != "admin":
        stmt = stmt.where(Order.user_id == user.id)
    if status_filter:
        stmt = stmt.where(Order.status == status_filter)
    return db.scalars(stmt.limit(limit).offset(offset)).all()


@app.get("/orders/{order_id}", response_model=OrderOut, tags=["Pedidos"])
def get_order(
    order_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    order = db.scalar(_order_query().where(Order.id == order_id))
    if not order or (user.role != "admin" and order.user_id != user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pedido não encontrado")
    return order


@app.patch("/orders/{order_id}/status", response_model=OrderOut, tags=["Pedidos"])
def update_order_status(
    order_id: int,
    data: OrderStatusUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    order = db.scalar(_order_query().where(Order.id == order_id))
    if not order or (user.role != "admin" and order.user_id != user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pedido não encontrado")

    if user.role != "admin":
        # Cliente só pode cancelar, e apenas antes do preparo
        if data.status != "cancelled" or order.status != "received":
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Você só pode cancelar pedidos que ainda estão como 'received'",
            )
    elif order.status in ("completed", "cancelled"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Pedido já está '{order.status}'")

    order.status = data.status
    db.commit()
    return order


if __name__ == "__main__":
    import uvicorn
    threading.Timer(1.5, lambda: webbrowser.open("http://localhost:8000")).start()
    uvicorn.run(app, host="127.0.0.1", port=8000)
