from flask import Flask
from call_handler import call_next_contact

app = Flask(__name__)

@app.route("/")
def home():
    return {"status": "Sara AI outbound bot is running 🚀"}

@app.route("/outbound")
def outbound():
    message = call_next_contact()
    return {"message": message}
