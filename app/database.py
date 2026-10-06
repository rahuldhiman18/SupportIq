import os
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

#for database engine
engine = create_engine(DATABASE_URL)

SessionLocal = sessionmaker(bind=engine,autoflush=False)
#bind=engine says session made by factory to talk his database
#autoflush=False stops SQLAlchemy from quietly sending pending changes to the database before every query

class Base(DeclarativeBase):
    pass

#check condition 
if not DATABASE_URL:
    raise RuntimeError("Database Url is not set,Add it to your .env file")