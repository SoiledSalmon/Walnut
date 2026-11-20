"""Start the Buy or Wait? web app: python main.py, then open http://127.0.0.1:8000"""
import uvicorn

from buyorwait.api import create_app

if __name__ == '__main__':
    uvicorn.run(create_app(), host='127.0.0.1', port=8000)
