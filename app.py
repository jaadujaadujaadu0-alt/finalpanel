import os
import re
import time
import html
import threading
from datetime import datetime, timedelta, time as datetime_time
import requests
from flask import Flask, render_template, request, redirect, url_for, session, flash
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)
app.secret_key = os.getenv(
    "SECRET_KEY",
    "super-secret-session-key-change-this"
)
DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL or "sqlite:///database.db"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# -------------------------------------------------------------------------
# Database Schema Definitions
# -------------------------------------------------------------------------
class AgentUser(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(50), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    allocated_numbers = db.relationship('SMSNumber', backref='assigned_agent', lazy=True)
    clients = db.relationship('ClientUser', backref='creator_agent', lazy=True)  # Link Agent to Clients

class ClientUser(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(50), nullable=False)
    agent_id = db.Column(db.Integer, db.ForeignKey('agent_user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    allocated_numbers = db.relationship('SMSNumber', backref='assigned_client', lazy=True) # Linked Client Numbers

class SMSNumber(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    num_range = db.Column(db.String(100))
    phone_number = db.Column(db.String(50), unique=True)
    fetched_at = db.Column(db.DateTime, default=datetime.utcnow)
    allocated_agent_id = db.Column(db.Integer, db.ForeignKey('agent_user.id'), nullable=True)
    allocated_client_id = db.Column(db.Integer, db.ForeignKey('client_user.id'), nullable=True) # Target Client field tracker

class OTPMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.String(50))
    number = db.Column(db.String(50))
    sender = db.Column(db.String(100))
    message = db.Column(db.Text)
    payout = db.Column(db.String(20))
    unique_hash = db.Column(db.String(64), unique=True)

# -------------------------------------------------------------------------
# Background Worker Threads (Kept intact)
# -------------------------------------------------------------------------
def run_number_storage_worker():
    BASE = "http://51.210.208.26/ints"
    LOGIN_URL = f"{BASE}/login"
    SIGNIN_URL = f"{BASE}/signin"
    DATA_URL = f"{BASE}/agent/res/data_smsnumbers.php"
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
    try:
        r = s.get(LOGIN_URL)
        match = re.search(r"What is\s+(\d+)\s*\+\s*(\d+)", r.text)
        if not match: return
        captcha = int(match.group(1)) + int(match.group(2))
        payload = {"username": "Ashok20", "password": "aura20", "capt": str(captcha)}
        resp = s.post(SIGNIN_URL, data=payload, headers={"Origin": "http://51.210.208.26", "Referer": LOGIN_URL})
        if "/agent/" not in resp.url: return
        current_start = 0
        total_records = 40081
        echo_counter = 2
        while current_start < total_records:
            params = {"frange": "", "fclient": "", "totnum": str(total_records), "sEcho": str(echo_counter),
                      "iColumns": "8", "sColumns": ",,,,,,,", "iDisplayStart": str(current_start), "iDisplayLength": "1000",
                      "iSortCol_0": "0", "sSortDir_0": "asc", "iSortingCols": "1", "_:": str(int(time.time() * 1000))}
            response = s.get(DATA_URL, params=params, headers={"X-Requested-With": "XMLHttpRequest"}, timeout=15)
            if response.status_code != 200:
                time.sleep(5)
                continue
            data = response.json()
            if "iTotalRecords" in data: total_records = int(data["iTotalRecords"])
            records = data.get('aaData', data.get('data', []))
            if not records: break
            with app.app_context():
                for row in records:
                    try:
                        clean_items = [html.unescape(re.sub(r'<[^>]+>', '', str(item))).strip() for item in row]
                        rng = clean_items[1] if len(clean_items) > 1 else ""
                        num = clean_items[3] if len(clean_items) > 3 else ""
                        if num:
                            exists = SMSNumber.query.filter_by(phone_number=num).first()
                            if not exists: db.session.add(SMSNumber(num_range=rng, phone_number=num))
                    except Exception: continue
                db.session.commit()
            current_start += 1000
            echo_counter += 1
            time.sleep(0.4)
    except Exception as e: print(f"Number worker error: {e}")

def run_otp_storage_worker():
    API_URL = "http://51.77.216.195/crapi/lamix/viewstats"
    API_KEY = "RE5PREdBUzRpY1JGgniCfVRwmUddY4FrdmFqZH1jmHt4d1dGiGpvgQ=="
    while True:
        try:
            query_params = {"token": API_KEY, "dt1": datetime.now().strftime("%Y-%m-%d 00:00:00"), "dt2": datetime.now().strftime("%Y-%m-%d 23:59:59"), "records": 50}
            response = requests.get(API_URL, params=query_params, timeout=10)
            if response.status_code == 200:
                items = response.json()
                if isinstance(items, list):
                    with app.app_context():
                        for item in items:
                            fingerprint = f"{item.get('dt')}_{item.get('num')}_{item.get('message')}"
                            exists = OTPMessage.query.filter_by(unique_hash=fingerprint).first()
                            if not exists:
                                db.session.add(OTPMessage(timestamp=item.get('dt'), number=item.get('num'), sender=item.get('cli'), message=item.get('message'), payout=item.get('payout'), unique_hash=fingerprint))
                        db.session.commit()
        except Exception as e: print(f"OTP Engine Error: {e}")
        time.sleep(3)

def start_workers():
    threading.Thread(target=run_otp_storage_worker, daemon=True).start()
    threading.Thread(target=run_number_storage_worker, daemon=True).start()

# -------------------------------------------------------------------------
# HTTP View Endpoints
# -------------------------------------------------------------------------
# -------------------------------------------------------------------------
# HTTP View Endpoints
# -------------------------------------------------------------------------
@app.route('/')
def index():
    if session.get('role') == 'admin': return redirect(url_for('agents_management'))
    elif session.get('role') == 'agent': return redirect(url_for('agent_report')) # Changed from agent_dashboard to agent_report
    elif session.get('role') == 'client': return redirect(url_for('client_dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username').strip()
        password = request.form.get('password').strip()
        
        # 1. Check Admin
        if username == "Ashok20" and password == "aura20":
            session.clear()
            session['logged_in'] = True; session['username'] = username; session['role'] = 'admin'
            return redirect(url_for('agents_management'))
            
        # 2. Check Agent
        agent = AgentUser.query.filter_by(username=username, password=password).first()
        if agent:
            session.clear()
            session['logged_in'] = True; session['agent_id'] = agent.id; session['username'] = agent.username; session['role'] = 'agent'
            return redirect(url_for('agent_report')) # Changed from agent_dashboard to agent_report
            
        # 3. Check Client
        client = ClientUser.query.filter_by(username=username, password=password).first()
        if client:
            session.clear()
            session['logged_in'] = True
            session['client_id'] = client.id
            session['parent_agent_id'] = client.agent_id  
            session['username'] = client.username
            session['role'] = 'client'
            return redirect(url_for('client_dashboard'))
            
        flash("Invalid Credentials Provided.")
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# --- ADMIN VIEW ENDPOINTS ---
@app.route('/admin/agents', methods=['GET', 'POST'])
def agents_management():
    if session.get('role') != 'admin': return redirect(url_for('login'))
    if request.method == 'POST':
        agent_user = request.form.get('username', '').strip()
        agent_pass = request.form.get('password', '').strip()
        if agent_user and agent_pass:
            if not AgentUser.query.filter_by(username=agent_user).first():
                db.session.add(AgentUser(username=agent_user, password=agent_pass))
                db.session.commit()
                flash(f"Agent '{agent_user}' successfully added.")
            else: flash("Error: Username already exists.")
    return render_template('agents.html', agents=AgentUser.query.order_by(AgentUser.id.desc()).all())

@app.route('/admin/allocations')
def admin_allocations():
    if session.get('role') != 'admin': return redirect(url_for('login'))
    search_query = request.args.get('search_number', '').strip()
    search_result = None
    if search_query:
        found_num = SMSNumber.query.filter_by(phone_number=search_query).first()
        if found_num:
            status_text = f"Allocated to Agent: '{AgentUser.query.get(found_num.allocated_agent_id).username}'" if found_num.allocated_agent_id else "Unallocated"
            search_result = {"phone_number": found_num.phone_number, "num_range": found_num.num_range, "status": status_text}
        else: search_result = {"phone_number": search_query, "status": "Not found"}
    
    agents_data = []
    for ag in AgentUser.query.all():
        r_groups = {}
        for item in SMSNumber.query.filter_by(allocated_agent_id=ag.id).all():
            r_groups.setdefault(item.num_range, []).append(item.phone_number)
        agents_data.append({"id": ag.id, "username": ag.username, "ranges": r_groups})
    return render_template('admin_allocations.html', agents=agents_data, search_result=search_result, search_query=search_query)

@app.route('/admin/stats')
def admin_stats():
    if session.get('role') != 'admin': return redirect(url_for('login'))
    q = "SELECT au.username, COUNT(om.id) as total_sms FROM agent_user au LEFT JOIN sms_number sn ON sn.allocated_agent_id = au.id LEFT JOIN otp_message om ON om.number = sn.phone_number GROUP BY au.id, au.username ORDER BY total_sms DESC"
    return render_template('admin_stats.html', stats_list=db.session.execute(db.text(q)).fetchall())

@app.route('/admin/numbers')
def numbers_view():
    if session.get('role') != 'admin': return redirect(url_for('login'))
    return render_template('numbers.html', pagination=SMSNumber.query.order_by(SMSNumber.id.desc()).paginate(page=request.args.get('page', 1, type=int), per_page=100))

@app.route('/admin/otps')
def otps_view():
    if session.get('role') != 'admin': return redirect(url_for('login'))
    return render_template('otps.html', pagination=OTPMessage.query.order_by(OTPMessage.id.desc()).paginate(page=request.args.get('page', 1, type=int), per_page=50))

# --- AGENT WORKSPACE WORKFLOWS ---
# --- AGENT WORKSPACE WORKFLOWS ---
@app.route('/agents')
def agent_dashboard():
    if session.get('role') != 'agent': return redirect(url_for('login'))
    return redirect(url_for('agent_report')) # Forces the fallback route to safely land on the report layout instead of agent_view.html


@app.route('/agents/allocate', methods=['GET', 'POST'])
def agent_allocate():
    if session.get('role') != 'agent': return redirect(url_for('login'))
    current_agent_id = session.get('agent_id')
    if request.method == 'POST':
        selected_range = request.form.get('num_range')
        qty = int(request.form.get('quantity', 0))
        if selected_range and qty > 0:
            avail = SMSNumber.query.filter(SMSNumber.num_range == selected_range, SMSNumber.allocated_agent_id == None).limit(qty).all()
            if len(avail) >= qty:
                for num in avail: num.allocated_agent_id = current_agent_id
                db.session.commit()
                flash(f"Allocated {qty} numbers successfully!")
            else: flash(f"Only {len(avail)} available left.")
    ranges = [r[0] for r in db.session.query(SMSNumber.num_range).filter(SMSNumber.allocated_agent_id == None).distinct().all() if r[0]]
    return render_template('agent_allocate.html', ranges=ranges)

@app.route('/agents/my-numbers')
def agent_my_numbers():
    if session.get('role') != 'agent': 
        return redirect(url_for('login'))
        
    agent_id = session.get('agent_id')
    page = request.args.get('page', 1, type=int)
    selected_range = request.args.get('range_filter', '')

    # 1. Pull unique ranges allocated to this agent profile for drop-down option rendering
    allocated_ranges_query = SMSNumber.query.filter_by(allocated_agent_id=agent_id).with_entities(SMSNumber.num_range).distinct().all()
    agent_allocated_ranges = [r[0] for r in allocated_ranges_query if r[0]]

    # 2. Re-build pipeline table search filter criteria
    query = SMSNumber.query.filter_by(allocated_agent_id=agent_id)
    
    if selected_range:
        query = query.filter_by(num_range=selected_range)
        
    pagination = query.order_by(SMSNumber.id.desc()).paginate(page=page, per_page=50, error_out=False)

    return render_template(
        'agent_my_numbers.html', 
        pagination=pagination, 
        agent_allocated_ranges=agent_allocated_ranges
    )


@app.route('/agents/clients', methods=['GET', 'POST'])
def agent_clients():
    if session.get('role') != 'agent': return redirect(url_for('login'))
    current_agent_id = session.get('agent_id')
    
    if request.method == 'POST':
        client_user = request.form.get('username', '').strip()
        client_pass = request.form.get('password', '').strip()
        
        if client_user and client_pass:
            exists = ClientUser.query.filter_by(username=client_user).first()
            if not exists:
                new_client = ClientUser(username=client_user, password=client_pass, agent_id=current_agent_id)
                db.session.add(new_client)
                db.session.commit()
                flash(f"Client User Account '{client_user}' Created Successfully!")
            else:
                flash("Error: That client username already exists inside system records.")
                
    my_clients = ClientUser.query.filter_by(agent_id=current_agent_id).order_by(ClientUser.id.desc()).all()
    return render_template('agent_clients.html', clients=my_clients)

# --- SECURE CLIENT PIPELINE VIEWS ---
@app.route('/client/dashboard')
def client_dashboard():
    if session.get('role') != 'client':
        return redirect(url_for('login'))
        
    client_id = session.get('client_id') or session.get('user_id')
    
    # 1. Fetch total allocated numbers count safely
    client_numbers_query = SMSNumber.query.filter_by(allocated_client_id=client_id).all()
    total_allocated_numbers = len(client_numbers_query)
    client_phone_numbers = [num.phone_number for num in client_numbers_query]
    
    total_today = 0
    total_7_days = 0
    total_30_days = 0
    
    if client_phone_numbers:
        now = datetime.utcnow()
        today_start = datetime.combine(now.date(), datetime_time.min)
        seven_days_ago = today_start - timedelta(days=7)
        thirty_days_ago = today_start - timedelta(days=30)
        
        # 2. Count metrics logs matching the extracted phone arrays
        total_today = OTPMessage.query.filter(OTPMessage.number.in_(client_phone_numbers), OTPMessage.timestamp >= today_start).count()
        total_7_days = OTPMessage.query.filter(OTPMessage.number.in_(client_phone_numbers), OTPMessage.timestamp >= seven_days_ago).count()
        total_30_days = OTPMessage.query.filter(OTPMessage.number.in_(client_phone_numbers), OTPMessage.timestamp >= thirty_days_ago).count()

    return render_template('client_dashboard.html', total_allocated_numbers=total_allocated_numbers, total_today=total_today, total_7_days=total_7_days, total_30_days=total_30_days)

@app.route('/clients/allocate', methods=['GET', 'POST'])
def client_allocate():
    if session.get('role') != 'client': return redirect(url_for('login'))
    client_id = session.get('client_id')
    parent_agent_id = session.get('parent_agent_id')
    
    if request.method == 'POST':
        selected_range = request.form.get('num_range')
        qty = int(request.form.get('quantity', 0))
        
        if selected_range and qty > 0:
            # STRICT FILTER: Line MUST belong to their specific parent agent AND have no active client allocation
            avail = SMSNumber.query.filter(
                SMSNumber.num_range == selected_range,
                SMSNumber.allocated_agent_id == parent_agent_id,
                SMSNumber.allocated_client_id == None
            ).limit(qty).all()
            
            if len(avail) >= qty:
                for num in avail: 
                    num.allocated_client_id = client_id
                db.session.commit()
                flash(f"Successfully allocated {qty} lines inside your client workspace profile!")
            else:
                flash(f"Allocation failure. Only {len(avail)} unallocated parent items remaining.")
                
    # Show only prefix pools available within the parent Agent's balance sheet
    ranges = [r[0] for r in db.session.query(SMSNumber.num_range).filter(
        SMSNumber.allocated_agent_id == parent_agent_id,
        SMSNumber.allocated_client_id == None
    ).distinct().all() if r[0]]
    
    return render_template('client_allocate.html', ranges=ranges)


@app.route('/clients/mynumbers')
def client_my_numbers():
    if session.get('role') != 'client':
        return redirect(url_for('login'))
        
    client_id = session.get('client_id')
    page = request.args.get('page', 1, type=int)
    selected_range = request.args.get('range_filter', '')

    # 1. Fetch unique ranges using 'allocated_client_id' instead of 'client_id'
    allocated_ranges_query = SMSNumber.query.filter_by(allocated_client_id=client_id).with_entities(SMSNumber.num_range).distinct().all()
    client_allocated_ranges = [r[0] for r in allocated_ranges_query if r[0]]

    # 2. Build the database query filtered for this specific client workspace profile
    query = SMSNumber.query.filter_by(allocated_client_id=client_id)
    
    # Apply dynamic filter selection if active
    if selected_range:
        query = query.filter_by(num_range=selected_range)
        
    pagination = query.order_by(SMSNumber.id.desc()).paginate(page=page, per_page=20, error_out=False)

    return render_template(
        'client_my_numbers.html', 
        pagination=pagination, 
        client_allocated_ranges=client_allocated_ranges
    )



@app.route('/clients/reports')
def client_reports():
    if session.get('role') != 'client': return redirect(url_for('login'))
    client_id = session.get('client_id')
    
    # Extract only clean phone numbers currently allocated to this client profile
    my_numbers_list = [n.phone_number for n in SMSNumber.query.filter_by(allocated_client_id=client_id).all()]
    
    page = request.args.get('page', 1, type=int)
    if my_numbers_list:
        pagination = OTPMessage.query.filter(OTPMessage.number.in_(my_numbers_list)).order_by(OTPMessage.id.desc()).paginate(page=page, per_page=50)
    else:
        pagination = None
        
    return render_template('client_reports.html', pagination=pagination)

@app.route('/agents/unallocate-client/<int:number_id>', methods=['POST'])
def agent_unallocate_client(number_id):
    if session.get('role') != 'agent': 
        return redirect(url_for('login'))
        
    current_agent_id = session.get('agent_id')
    num = SMSNumber.query.filter_by(id=number_id, allocated_agent_id=current_agent_id).first_or_404()
    
    if num.allocated_client_id:
        num.allocated_client_id = None
        db.session.commit()
        flash("Number successfully unallocated from client!")
    else:
        flash("This number is already unallocated.")
        
    return redirect(url_for('agent_my_numbers', page=request.args.get('page', 1, type=int)))

@app.route('/agents/report')
def agent_report():
    if session.get('role') != 'agent':
        return redirect(url_for('login'))
        
    current_agent_id = session.get('agent_id')
    page = request.args.get('page', 1, type=int)
    
    agent_numbers = [num.phone_number for num in SMSNumber.query.filter_by(allocated_agent_id=current_agent_id).all()]
    
    pagination = OTPMessage.query.filter(OTPMessage.number.in_(agent_numbers))\
                                 .order_by(OTPMessage.timestamp.desc())\
                                 .paginate(page=page, per_page=15, error_out=False)
                             
    return render_template('agent_report.html', pagination=pagination)

# -------------------------------------------------------------------------
# Dynamic Schema Alteration & Bootstrapping
# -------------------------------------------------------------------------
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        # Safe migration patch check for allocated_agent_id
        try: db.session.execute(db.text("SELECT allocated_agent_id FROM sms_number LIMIT 1"))
        except Exception:
            db.session.rollback()
            db.session.execute(db.text("ALTER TABLE sms_number ADD COLUMN allocated_agent_id INTEGER REFERENCES agent_user(id)"))
            db.session.commit()
            
        # Safe migration patch check for allocated_client_id 
        try: db.session.execute(db.text("SELECT allocated_client_id FROM sms_number LIMIT 1"))
        except Exception:
            db.session.rollback()
            db.session.execute(db.text("ALTER TABLE sms_number ADD COLUMN allocated_client_id INTEGER REFERENCES client_user(id)"))
            db.session.commit()
            
        try: db.session.execute(db.text("SELECT id FROM client_user LIMIT 1"))
        except Exception:
            db.session.rollback()
            db.create_all()
            
    start_workers()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
