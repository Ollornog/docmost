import traceback
from django.contrib.auth import get_user_model
from baserow.contrib.database.models import Database
from baserow.contrib.database.table.models import Table
from baserow.contrib.database.table.handler import TableHandler
from baserow.contrib.database.fields.handler import FieldHandler
from baserow.contrib.database.rows.handler import RowHandler
from baserow.contrib.database.views.handler import ViewHandler
U=get_user_model(); u=U.objects.get(email="kbsync@brdl.local"); rh=RowHandler(); vh=ViewHandler()
def T(i): return Table.objects.get(id=i)
def fmap(t): return {f.name:"field_%d"%f.id for f in t.field_set.all()}   # name -> "field_X"
def gv(row,key): return getattr(row,key) or ""
def lv(row,key): return list(getattr(row,key).all())
try:
    db=Database.objects.get(id=159)
    KUN=T(509); TYP=T(510); GER=T(513); REG=T(514)
    kf=fmap(KUN); tf=fmap(TYP); gf=fmap(GER); rf=fmap(REG)
    SM=Table.objects.filter(database=db, name="Soll-Matrix").first()
    if not SM:
        SM=TableHandler().create_table_and_fields(u, db, name="Soll-Matrix", fields=[
            ("Zweck","text",{}),("Ebene","text",{}),("Bereich","text",{}),("Gerät","text",{}),("Geräte-IP","text",{}),
            ("Richtung","text",{}),("Quelle","text",{}),("Ziel-System","text",{}),("Ziel-Hostname","text",{}),
            ("Ziel-IP","text",{}),("Ports","text",{}),("Protokoll","text",{}),("Anmerkung","text",{})])
    if not SM.field_set.filter(name="Kunde").exists():
        FieldHandler().create_field(u, SM, type_name="link_row", name="Kunde", link_row_table_id=KUN.id)
    sf=fmap(SM)
    M=SM.get_model(); old=list(M.objects.values_list("id",flat=True))
    if old: rh.delete_rows(u, SM, old)
    typname={t.id:gv(t,tf["Name"]) for t in TYP.get_model().objects.all()}
    regeln=list(REG.get_model().objects.all()); kunden=list(KUN.get_model().objects.all()); geraete=list(GER.get_model().objects.all())
    def base(rg): return {k:gv(rg,rf[v]) for k,v in [("Zweck","Zweck"),("Bereich","Bereich"),("Richtung","Richtung"),
        ("Quelle","Quelle"),("ZS","Ziel-System"),("ZH","Ziel-Hostname"),("ZI","Ziel-IP"),("Ports","Ports"),("Proto","Protokoll"),("Anm","Anmerkung")]}
    rows=[]
    def emit(kid,ebene,b,geraet="",gip=""):
        rows.append({sf["Zweck"]:b["Zweck"],sf["Ebene"]:ebene,sf["Bereich"]:b["Bereich"],sf["Gerät"]:geraet,sf["Geräte-IP"]:gip,
            sf["Richtung"]:b["Richtung"],sf["Quelle"]:b["Quelle"],sf["Ziel-System"]:b["ZS"],sf["Ziel-Hostname"]:b["ZH"],
            sf["Ziel-IP"]:b["ZI"],sf["Ports"]:b["Ports"],sf["Protokoll"]:b["Proto"],sf["Anmerkung"]:b["Anm"],sf["Kunde"]:[kid]})
    for k in kunden:
        kid=k.id
        kdev=[d for d in geraete if any(x.id==kid for x in lv(d,gf["Kunde"]))]
        dev_by_type={}
        for d in kdev:
            for ty in lv(d,gf["Gerätetyp"]): dev_by_type.setdefault(typname.get(ty.id,""),[]).append(d)
        for rg in regeln:
            gelt=gv(rg,rf["Geltung"]); b=base(rg)
            if gelt=="Allgemein": emit(kid,"Allgemein",b)
            elif gelt=="Gerätegruppe":
                grp=lv(rg,rf["Gerätegruppe"]); gname=typname.get(grp[0].id) if grp else None
                for d in dev_by_type.get(gname,[]): emit(kid,"Gerätegruppe (%s)"%gname,b,gv(d,gf["Name"]),gv(d,gf["IP-Adresse"]))
            elif gelt=="Kunde":
                if any(x.id==kid for x in lv(rg,rf["Kunde"])): emit(kid,"Kundenspezifisch",b)
    if rows: rh.create_rows(u, SM, rows_values=rows)
    print("Soll-Matrix Zeilen: %d"%len(rows))
    have={v.name for v in SM.view_set.all()}
    kfield=SM.field_set.get(name="Kunde")
    for k in kunden:
        nm="Soll-Matrix – %s"%gv(k,kf["Name"])
        if nm in have: continue
        v=vh.create_view(u, SM, type_name="grid", name=nm)
        vh.create_filter(u, v, field=kfield, type_name="link_row_has", value=str(k.id))
        print("View:",nm)
    print("GEN OK")
except Exception as e:
    print("GEN_ERR:",repr(e)); traceback.print_exc()
