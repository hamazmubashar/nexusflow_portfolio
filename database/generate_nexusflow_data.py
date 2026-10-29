#!/usr/bin/env python3
"""Deterministic fictional NexusFlow snapshot. Python 3.10+, standard library only.

Usage: python generate_nexusflow_data.py --output-dir ./nexusflow_csv --seed 42
Load schema.sql first; import CSV in dependency order from manifest.json.
Snapshot: 2026-10-28. No real personal information is generated.
"""
import argparse
import csv
import json
import random
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

SNAP = date(2026, 10, 28)
UTC = timezone.utc
DEPTS = [
    ('DPT-0001', 'Engineering', [('VP Engineering',1),('Engineering Manager',3),('Software Engineer',21),('Site Reliability Engineer',4),('QA Engineer',4),('Product Manager',3),('UX Designer',2)]),
    ('DPT-0002', 'Sales', [('VP Sales',1),('Sales Manager',2),('Account Executive',9),('Sales Development Representative',4),('Sales Operations Analyst',2)]),
    ('DPT-0003', 'Customer Success/Support', [('VP Customer Success',1),('Customer Success Manager - Lead',1),('Support Manager',1),('Customer Success Manager',7),('Support Engineer',7)]),
    ('DPT-0004', 'Operations/HR', [('Director of Operations & People',1),('HR Business Partner',2),('IT Administrator',2),('Security & Compliance Analyst',2),('Workplace Coordinator',1)]),
    ('DPT-0005', 'Finance', [('Finance Director',1),('Controller',1),('Billing Analyst',1),('FP&A Analyst',1)]),
]
PLAN = {'SMB':('STARTER', Decimal('600'),50,249), 'MID_MARKET':('GROWTH',Decimal('300'),250,999), 'ENTERPRISE':('ENTERPRISE',Decimal('140'),1000,10000)}
FIELDS = {
'departments':['department_id','name','headcount','manager_id'],
'employees':['employee_id','first_name','last_name','email','department_id','role','salary','hire_date','status'],
'products':['product_id','name','module_code','base_price_per_seat','description'],
'customers':['customer_id','company_name','domain','tier','total_seats','status','created_at','csm_employee_id'],
'contracts':['contract_id','customer_id','plan_tier','start_date','end_date','annual_recurring_revenue','auto_renew','status'],
'invoices':['invoice_id','contract_id','customer_id','issue_date','due_date','amount','status'],
'payments':['payment_id','invoice_id','payment_date','amount','payment_method','status'],
'support_tickets':['ticket_id','customer_id','created_by_email','assigned_employee_id','severity','subject','status','created_at','resolved_at'],
'assets':['asset_id','customer_id','device_name','os_type','serial_number','status','last_sync_at']}
FIRST = ['Alex','Jordan','Morgan','Taylor','Riley','Casey','Avery','Sam','Jamie','Quinn','Drew','Skyler','Reese','Parker','Cameron','Rowan','Hayden']
LAST = ['Stone','Rivera','Chen','Patel','Nguyen','Brooks','Kim','Bennett','Diaz','Morgan','Singh','Turner','Lee','Campbell','Foster','Wright','Adams']

def key(prefix, n, digits=4):
    if not 1 <= n <= 10**digits-1: raise ValueError(f'{prefix} ID namespace exhausted')
    return f'{prefix}-{n:0{digits}d}'

def ts(d, hour=12): return datetime(d.year,d.month,d.day,hour,tzinfo=UTC).isoformat(timespec='seconds').replace('+00:00','Z')
def money(d): return f'{d:.2f}'
def pickdate(rng, earliest, latest): return earliest + timedelta(days=rng.randint(0,(latest-earliest).days))

def build(seed):
    rng=random.Random(seed)
    tables={k:[] for k in FIELDS}
    by_role=defaultdict(list)
    for depid,depname,roles in DEPTS:
        manager=None
        for role,n in roles:
            for _ in range(n):
                i=len(tables['employees'])+1
                empid=key('EMP',i)
                first=FIRST[(i-1)%len(FIRST)]; last=LAST[((i-1)*7)%len(LAST)]
                email=f'{first.lower()}.{last.lower()}.{i:04d}@nexusflow.example'
                salary={'Engineering':125000,'Sales':105000,'Customer Success/Support':85000,'Operations/HR':90000,'Finance':110000}[depname]
                salary += 30000 if manager is None else (15000 if 'Manager' in role or 'Lead' in role else 0)
                hire=pickdate(rng,date(2021,2,1),date(2026,8,31))
                tables['employees'].append(dict(employee_id=empid,first_name=first,last_name=last,email=email,department_id=depid,role=role,salary=money(Decimal(salary)),hire_date=hire.isoformat(),status='ACTIVE'))
                by_role[role].append(empid)
                if manager is None: manager=empid
        tables['departments'].append(dict(department_id=depid,name=depname,headcount=sum(n for _,n in roles),manager_id=manager))
    for n,(name,desc) in enumerate([
        ('AssetTracker','Asset inventory, discovery and lifecycle management'),
        ('LicenseOptimizer','License entitlement and utilization reconciliation'),
        ('SecurityShield','Security findings and policy risk prioritization')],1):
        tables['products'].append(dict(product_id=key('PRD',n),name=name,module_code=name.upper(),base_price_per_seat=money(Decimal('0')),description=desc+'; bundled with all plans, no standalone charge'))
    # Exact integer seat solution: 6962*600 + 19000*300 + 18020*140 = 12,400,000.
    seats = [('SMB',100)]*69 + [('SMB',62)] + [('MID_MARKET',500)]*38 + [('ENTERPRISE',1500)]*11 + [('ENTERPRISE',1520)]
    assert len(seats)==120
    rng.shuffle(seats)
    csms=by_role['Customer Success Manager']+by_role['Customer Success Manager - Lead']
    support=by_role['Support Engineer']+[by_role['Support Manager'][0]]
    # Six delinquent but still contracted accounts: no historical churn implied.
    delinquent=set(rng.sample(range(1,121),6))
    for i,(tier,seatcount) in enumerate(seats,1):
        cid=key('CUST',i); ctr=key('CTR',i)
        plan,unit,lo,hi=PLAN[tier]
        assert lo<=seatcount<=hi
        domain=f'client{i:04d}.example'
        # One current 12-month prepaid term per account, all active at snapshot.
        start=pickdate(rng,date(2026,1,1),date(2026,9,20) if i in delinquent else date(2026,10,1))
        end=date(start.year+1,start.month,start.day)
        arr=unit*seatcount
        tables['customers'].append(dict(customer_id=cid,company_name=f'Nexus Client {i:04d} LLC',domain=domain,tier=tier,total_seats=seatcount,status='DELINQUENT' if i in delinquent else 'ACTIVE',created_at=ts(start-timedelta(days=rng.randint(10,120))),csm_employee_id=csms[(i-1)%len(csms)]))
        tables['contracts'].append(dict(contract_id=ctr,customer_id=cid,plan_tier=plan,start_date=start.isoformat(),end_date=end.isoformat(),annual_recurring_revenue=money(arr),auto_renew='true',status='ACTIVE'))
        inv_id=f'INV-{start.year}-{i:04d}'
        due=start+timedelta(days=30)
        # Invoice issued at contract start, paid promptly except delinquent accounts.
        tables['invoices'].append(dict(invoice_id=inv_id,contract_id=ctr,customer_id=cid,issue_date=start.isoformat(),due_date=due.isoformat(),amount=money(arr),status='OVERDUE' if i in delinquent else 'PAID'))
        if i not in delinquent:
            paid=start+timedelta(days=rng.randint(1,20))
            tables['payments'].append(dict(payment_id=key('PAY',len(tables['payments'])+1),invoice_id=inv_id,payment_date=paid.isoformat(),amount=money(arr),payment_method=rng.choice(['ACH','WIRE','CARD']),status='SETTLED'))
        # 3-7 representative discovered assets, not all 46k contracted billable seats.
        for j in range(rng.randint(3,7)):
            k=len(tables['assets'])+1
            sync=pickdate(rng,max(start,SNAP-timedelta(days=20)),SNAP)
            tables['assets'].append(dict(asset_id=key('AST',k),customer_id=cid,device_name=f'NF-{i:04d}-DEV-{j+1:03d}',os_type=rng.choice(['WINDOWS','MACOS','LINUX']),serial_number=f'NFSYN-{k:08d}',status=rng.choice(['ACTIVE','ACTIVE','ACTIVE','RETIRED']),last_sync_at=ts(sync)))
        for j in range(rng.randint(2,5)):
            k=len(tables['support_tickets'])+1
            sev='P1' if (i<=4 and j==0) else ('P2' if (i<=12 and j==0) else rng.choice(['P2','P3','P3','P4']))
            # Force open P1/P2 incidents near snapshot to avoid artificial SLA violations.
            open_case=(i<=12 and j==0) or rng.random()<0.18
            if open_case:
                created=SNAP-timedelta(days=0 if sev in ('P1','P2') else rng.randint(0,5))
                state=rng.choice(['NEW','TRIAGED','IN_PROGRESS'])
                resolved=''
            else:
                created=pickdate(rng,start,SNAP-timedelta(days=1))
                state=rng.choice(['RESOLVED','CLOSED'])
                resolved=ts(min(created+timedelta(days=rng.randint(0,1)),SNAP),18)
            tables['support_tickets'].append(dict(ticket_id=key('TK',k),customer_id=cid,created_by_email=f'it-admin@{domain}',assigned_employee_id=support[(k-1)%len(support)],severity=sev,subject=f'{sev} {rng.choice(["Inventory synchronization","License reconciliation","Access issue","Security finding"])}',status=state,created_at=ts(created,9),resolved_at=resolved))
    validate(tables)
    return tables

def validate(t):
    assert len(t['customers'])==120 and len(t['employees'])==85 and len(t['departments'])==5
    assert Counter(c['tier'] for c in t['customers'])=={'SMB':70,'MID_MARKET':38,'ENTERPRISE':12}
    assert {d['name']:int(d['headcount']) for d in t['departments']}=={'Engineering':38,'Sales':18,'Customer Success/Support':17,'Operations/HR':8,'Finance':4}
    assert sum((Decimal(c['annual_recurring_revenue']) for c in t['contracts']),Decimal(0))==Decimal('12400000.00')
    assert len(t['contracts'])==120 and all(c['status']=='ACTIVE' for c in t['contracts'])
    assert sum(c['status']=='DELINQUENT' for c in t['customers'])==6
    assert sum(i['status']=='OVERDUE' for i in t['invoices'])==6
    assert any(x['severity']=='P1' and not x['resolved_at'] for x in t['support_tickets'])
    assert any(x['severity']=='P2' and not x['resolved_at'] for x in t['support_tickets'])
    keys={name:set() for name in t}
    for name,rows in t.items():
        pk=FIELDS[name][0]
        for row in rows:
            assert set(row)==set(FIELDS[name]), (name,row)
            assert row[pk] not in keys[name],(name,row[pk]); keys[name].add(row[pk])
            prefix={'departments':'DPT','employees':'EMP','products':'PRD','customers':'CUST','contracts':'CTR','invoices':None,'payments':'PAY','support_tickets':'TK','assets':'AST'}[name]
            regex=r'INV-20\d{2}-\d{4}' if name=='invoices' else rf'{prefix}-\d{{4}}'
            assert re.fullmatch(regex,row[pk]),(name,row[pk])
    emp={e['employee_id']:e for e in t['employees']}
    dep={d['department_id']:d for d in t['departments']}
    customers={c['customer_id']:c for c in t['customers']}
    contracts={c['contract_id']:c for c in t['contracts']}
    invoices={i['invoice_id']:i for i in t['invoices']}
    for d in dep.values():
        assert sum(e['department_id']==d['department_id'] for e in emp.values())==int(d['headcount'])
        assert emp[d['manager_id']]['department_id']==d['department_id']
    for c in customers.values():
        assert c['csm_employee_id'] in emp and emp[c['csm_employee_id']]['department_id']=='DPT-0003'
    for c in contracts.values():
        assert c['customer_id'] in customers
        cust=customers[c['customer_id']]; plan,unit,lo,hi=PLAN[cust['tier']]
        assert c['plan_tier']==plan and Decimal(c['annual_recurring_revenue'])==unit*int(cust['total_seats'])
        assert lo<=int(cust['total_seats'])<=hi
        assert date.fromisoformat(c['start_date'])<=SNAP<date.fromisoformat(c['end_date'])
    for i in invoices.values():
        assert i['contract_id'] in contracts and contracts[i['contract_id']]['customer_id']==i['customer_id']
        assert date.fromisoformat(i['issue_date'])<=date.fromisoformat(i['due_date'])
        assert Decimal(i['amount'])==Decimal(contracts[i['contract_id']]['annual_recurring_revenue'])
        assert (i['status']=='OVERDUE')==(customers[i['customer_id']]['status']=='DELINQUENT')
        if i['status']=='OVERDUE': assert date.fromisoformat(i['due_date'])<SNAP
    for p in t['payments']:
        assert p['invoice_id'] in invoices and invoices[p['invoice_id']]['status']=='PAID'
        assert Decimal(p['amount'])==Decimal(invoices[p['invoice_id']]['amount'])
        assert date.fromisoformat(invoices[p['invoice_id']]['issue_date'])<=date.fromisoformat(p['payment_date'])<=SNAP
    for x in t['support_tickets']:
        assert x['customer_id'] in customers and x['assigned_employee_id'] in emp
        assert (x['resolved_at']!='')==(x['status'] in ('RESOLVED','CLOSED'))
        if x['resolved_at']: assert x['created_at']<=x['resolved_at']
    for x in t['assets']:
        assert x['customer_id'] in customers and x['last_sync_at']<=ts(SNAP,23)
    return True

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output-dir',default='nexusflow_csv')
    ap.add_argument('--seed',type=int,default=42)
    args=ap.parse_args()
    tables=build(args.seed)
    out=Path(args.output_dir);out.mkdir(parents=True,exist_ok=True)
    for name,rows in tables.items():
        with (out/f'{name}.csv').open('w',newline='',encoding='utf-8') as fh:
            writer=csv.DictWriter(fh,fieldnames=FIELDS[name]);writer.writeheader();writer.writerows(rows)
    manifest={'snapshot_date':SNAP.isoformat(),'seed':args.seed,'currency':'USD','arr':'12400000.00','table_row_counts':{k:len(v) for k,v in tables.items()},'import_order':list(FIELDS),'notes':['Fictional synthetic records','Six delinquent accounts remain under ACTIVE contracts','Asset sample is not a seat-by-seat inventory','Customer distribution supersedes specification v1.0 distribution; original four-digit TK keys retained']}
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(manifest,indent=2))

if __name__=='__main__': main()
