import requests
import time
from datetime import date

list = ['BEXIMCO', 'PHARMAID', 'SEAPEARL', 'APEXSPINN', 'AMBEEPHA', 'REGENTTEX', 'DESCO', 
    'MEGHNAPET', 'USMANIAGL', 'OLYMPIC', 'NURANI', 'MONOSPOOL', 'JMISMDL', 'ATLASBANG', 
    'APEXFOODS', 'BSCPLC', 'TITASGAS', 'NFML', 'POWERGRID',
    
    'EASTRNLUB', 'APOLOISPAT', 'EMERALDOIL', 'KPPL', 'RINGSHINE', 'JUTESPINN', 'RSRMSTEEL', 
    'RENWICKJA', 'RAHIMAFOOD', 'RNSPIN', 'MPETROLEUM', 'NEWLINE', 'RAHIMTEXT', 'BDPAINTS', 
    'MASTERAGRO', 'NORTHERN', 'HAMI',

    'ADNTEL', 'ACTIVEFINE', 'WALTONHIL', 'ACIFORMULA', 'APEXTANRY', 'GEMINISEA', 'BPML', 
    'WATACHEM', 'ANLIMAYARN', 'STANCERAM', 'YPL', 'ECABLES', 'DSHGARME', 'NTLTUBES', 
    'BDAUTOCA', 'SAIFPOWER', 'EHL', 'APEXFOOT', 'CENTRALPHL', 'CAPMIBBLMF', 'MLDYEING', 
    'MKFOOTWEAR', 'TECHNODRUG', 'SALAMCRST', 'GHCL', 'ALIF', 'LHB', 'BXPHARMA', 'FORTUNE', 
    'MATINSPINN', 'FARCHEM', 'BANGAS',

    'AOPLC', 'TAMIJTEX', 'CNATEX', 'HWAWELLTEX', 'HFL', 'BEACONPHAR', 'STYLECRAFT', 
    'DAFODILCOM', 'HEIDELBCEM', 'AZIZPIPES', 'AMPL', 'AFTABAUTO', 'METROSPIN', 'BDWELDING', 
    'NTC', 'PADMAOIL', 'SONARGAON', 'RENATA', 'BSC', 'SAFKOSPINN', 'ACMELAB', 'BSRMLTD', 
    'LEGACYFOOT', 'CVOPRL', 'TALLUSPIN', 'AMCL(PRAN)', 'ANWARGALV', 'SQUARETEXT', 
    'MEGCONMILK', 'KOHINOOR', 'SHARPIND', 'OAL', 'MODERNDYE', 'SHEPHERD', 'SINGERBD',

    'NAVANAPHAR', 'CONFIDCEM', 'IFADAUTOS', 'GHAIL', 'BDLAMPS', 'ENVOYTEX', 'BARKAPOWER', 
    'OIMEX', 'MAKSONSPIN', 'KTL', 'MJLBD', 'GENEXIL', 'AOL', 'SILCOPHL', 'IBP', 'INDEXAGRO', 
    'COPPERTECH', 'TOSRIFA', 'AFCAGRO', 'MALEKSPIN', 'LOVELLO', 'RAKCERAMIC', 'RUNNERAUTO', 
    'MIRAKHTER', 'BDTHAI', 'PDL', 'BDTHAIFOOD', 'MONNOAGML', 'EPGL', 'SAMATALETH', 
    'GBBPOWER', 'AMANFEED',

    'SIPLC', 'SHASHADNIM', 'FUWANGCER', 'UNIQUEHRL', 'ORIONINFU', 'ARGONDENIM', 
    'ORIONPHARM', 'NPOLYMER', 'RDFOOD', 'LRBDL', 'SHYAMPSUG', 'ACFL', 'CROWNCEMNT', 
    'ADVENT', 'BDCOM', 'SILVAPHL', 'KDSALTD', 'MONNOCERA', 'GOLDENSON', 'BBS', 'KPCL', 
    'GPHISPAT', 'DACCADYE', 'MIRACLEIND', 'ISLAMIINS', 'FEKDIL', 'SPCL', 'DOREENPWR', 
    'SINOBANGLA', 'SONALIPAPR', 'ALLTEX', 'BENGALWTL',

    'MEGHNACEM', 'SKTRIMS', 'SPCERAMICS', 'AGNISYSL', 'SSSTEEL', 'BPPL', 'SAMORITA', 
    'IBNSINA', 'AIL', 'PRIMETEX', 'UPGDCL', 'KAY&QUE', 'HAKKANIPUL', 'FUWANGFOOD', 
    'IBBLPBOND', 'BEACHHATCH', 'BNICL', 'QUASEMIND', 'SONALIANSH', 'ARAMITCEM', 
    'AL-HAJTEX', 'JHRML', 'MARICO', 'ITC', 'ARAMIT', 'NAVANACNG', 'ACMEPL', 'BSRMSTEEL', 
    'VFSTDL', 'PREMIERCEM', 'ZEALBANGLA', 'ZAHEENSPIN', 'DESHBANDHU', 'QUEENSOUTH', 
    'DSSL',

    'NAHEEACP', 'SALVOCHEM', 'ETL', 'DOMINAGE', 'INTECH', 'DULAMIACOT', 'MHSML', 
    'BESTHLDNG', 'WMSHIPYARD', 'AAMRATECH', 'SAIHAMCOT', 'SAIHAMTEX', 'ESQUIRENIT', 
    'RANFOUNDRY', 'PTL', 'EGEN', 'GQBALLPEN', 'MONNOFABR', 'AAMRANET', 'SIMTEX', 
    'SAPORTL', 'ZAHINTEX', 'BBSCABLES', 'KBPPWBIL', 'FINEFOODS', 'ROBI', 'PENINSULA', 
    'INTRACO', 'ISNLTD', 'GENNEXT'
]
# lOW CAP
list = ['PHARMAID', 'SEAPEARL', 'APEXSPINN', 'AMBEEPHA', 'REGENTTEX', 'DESCO', 
    'MEGHNAPET', 'USMANIAGL', 'OLYMPIC', 'NURANI', 'MONOSPOOL', 'JMISMDL', 'ATLASBANG', 
    'APEXFOODS', 'BSCPLC', 'TITASGAS', 'NFML', 'POWERGRID',
    
    'EASTRNLUB', 'APOLOISPAT', 'EMERALDOIL', 'KPPL', 'RINGSHINE', 'JUTESPINN', 'RSRMSTEEL', 
    'RENWICKJA', 'RAHIMAFOOD', 'RNSPIN', 'MPETROLEUM', 'NEWLINE', 'RAHIMTEXT', 'BDPAINTS', 
    'MASTERAGRO', 'NORTHERN', 'HAMI',

    'ADNTEL', 'ACTIVEFINE', 'WALTONHIL', 'ACIFORMULA', 'APEXTANRY', 'GEMINISEA', 'BPML', 
    'WATACHEM', 'ANLIMAYARN', 'STANCERAM', 'YPL', 'ECABLES', 'DSHGARME', 'NTLTUBES', 
    'BDAUTOCA', 'SAIFPOWER', 'EHL', 'APEXFOOT', 'CENTRALPHL', 'CAPMIBBLMF', 'MLDYEING', 
    'MKFOOTWEAR', 'TECHNODRUG', 'SALAMCRST', 'GHCL', 'ALIF', 'LHB', 'BXPHARMA', 'FORTUNE', 
    'MATINSPINN', 'FARCHEM', 'BANGAS',

    'AOPLC', 'TAMIJTEX', 'CNATEX', 'HWAWELLTEX', 'HFL', 'BEACONPHAR', 'STYLECRAFT', 
    'DAFODILCOM', 'HEIDELBCEM', 'AZIZPIPES', 'AMPL', 'AFTABAUTO', 'METROSPIN', 'BDWELDING', 
    'NTC', 'PADMAOIL', 'SONARGAON', 'RENATA', 'BSC', 'SAFKOSPINN', 'ACMELAB', 'BSRMLTD', 
    'LEGACYFOOT', 'CVOPRL', 'TALLUSPIN', 'AMCL(PRAN)', 'ANWARGALV', 'SQUARETEXT', 
    'MEGCONMILK', 'KOHINOOR', 'SHARPIND', 'OAL', 'MODERNDYE', 'SHEPHERD', 'SINGERBD',

    'NAVANAPHAR', 'CONFIDCEM', 'IFADAUTOS', 'GHAIL', 'BDLAMPS', 'ENVOYTEX', 'BARKAPOWER', 
    'OIMEX', 'MAKSONSPIN', 'KTL', 'MJLBD', 'GENEXIL', 'AOL', 'SILCOPHL', 'IBP', 'INDEXAGRO', 
    'COPPERTECH', 'TOSRIFA', 'AFCAGRO', 'MALEKSPIN', 'LOVELLO', 'RAKCERAMIC', 'RUNNERAUTO', 
    'MIRAKHTER', 'BDTHAI', 'PDL', 'BDTHAIFOOD', 'MONNOAGML', 'EPGL', 'SAMATALETH', 
    'GBBPOWER', 'AMANFEED',

    'SIPLC', 'SHASHADNIM', 'FUWANGCER', 'UNIQUEHRL', 'ORIONINFU', 'ARGONDENIM', 
    'ORIONPHARM', 'NPOLYMER', 'RDFOOD', 'LRBDL', 'SHYAMPSUG', 'ACFL', 'CROWNCEMNT', 
    'ADVENT', 'BDCOM', 'SILVAPHL', 'KDSALTD', 'MONNOCERA', 'GOLDENSON', 'BBS', 'KPCL', 
    'GPHISPAT', 'DACCADYE', 'MIRACLEIND', 'ISLAMIINS', 'FEKDIL', 'SPCL', 'DOREENPWR', 
    'SINOBANGLA', 'SONALIPAPR', 'ALLTEX', 'BENGALWTL',

    'MEGHNACEM', 'SKTRIMS', 'SPCERAMICS', 'AGNISYSL', 'SSSTEEL', 'BPPL', 'SAMORITA', 
    'IBNSINA', 'AIL', 'PRIMETEX', 'UPGDCL', 'KAY&QUE', 'HAKKANIPUL', 'FUWANGFOOD', 
    'IBBLPBOND', 'BEACHHATCH', 'BNICL', 'QUASEMIND', 'SONALIANSH', 'ARAMITCEM', 
    'AL-HAJTEX', 'JHRML', 'MARICO', 'ITC', 'ARAMIT', 'NAVANACNG', 'ACMEPL', 'BSRMSTEEL', 
    'VFSTDL', 'PREMIERCEM', 'ZEALBANGLA', 'ZAHEENSPIN', 'DESHBANDHU', 'QUEENSOUTH', 
    'DSSL',

    'NAHEEACP', 'SALVOCHEM', 'ETL', 'DOMINAGE', 'INTECH', 'DULAMIACOT', 'MHSML', 
    'BESTHLDNG', 'WMSHIPYARD', 'AAMRATECH', 'SAIHAMCOT', 'SAIHAMTEX', 'ESQUIRENIT', 
    'RANFOUNDRY', 'PTL', 'EGEN', 'GQBALLPEN', 'MONNOFABR', 'AAMRANET', 'SIMTEX', 
    'SAPORTL', 'ZAHINTEX', 'BBSCABLES', 'KBPPWBIL', 'FINEFOODS', 'ROBI', 'PENINSULA', 
    'INTRACO', 'ISNLTD', 'GENNEXT'
]

list = ['AIBL1STIMF', 'CAPMIBBLMF', 'IFILISLMF1', 'ADVENT', 'AGNISYSL', 'AIL', 'ALIF', 
    'APEXFOODS', 'ARAMIT', 'BDAUTOCA', 'BEACHHATCH', 'CAPMIBBLMF', 'DSSL', 'EASTRNLUB', 
    'EHL', 'EXIM1STMF', 'FAREASTLIF', 'FINEFOODS', 'GBBPOWER', 'HEIDELBCEM', 'HWAWELLTEX', 
    'INTRACO', 'ISLAMIINS', 'JAMUNAOIL', 'KBPPWBIL', 'KOHINOOR', 'KPCL', 'LEGACYFOOT', 
    'LHB', 'LINDEBD', 'RAHIMAFOOD', 'RENWICKJA', 'SAMATALETH', 'SEMLIBBLSF', 'SILCOPHL',
    'SKTRIMS', 'STANCERAM', 'TILIL', 'USMANIAGL']
list30 = ['BEACONPHAR', 'BSC', 'BSCPLC', 'BXPHARMA', 'GP', 'GPHISPAT', 'JAMUNAOIL', 'KBPPWBIL', 
    'LHB', 'LOVELLO', 'MJLBD', 'OLYMPIC', 'PADMAOIL', 'ROBI', 'SQURPHARMA', 'UNIQUEHRL']
list = list + list30    

list = ['INTECH', 'ESQUIRENIT', 'SPCERAMICS', 'BDTHAIFOOD', 'PRIMELIFE', 'PRIMETEX', 'NTC', 'SHEPHERD', 'HAMI', 'DULAMIACOT', 
    'ASIATICLAB', 'SHARPIND', 'YPL', 'ISNLTD', 'STANCERAM', 'AIL', 'MEGCONMILK', 'FARCHEM', 'ALARABANK', 'ACFL', 'INDEXAGRO', 
    'BESTHLDNG', 'LEGACYFOOT', 'MIRACLEIND', 'NFML', 'AOL', 'EPGL', 'SEAPEARL', 'LOVELLO', 'SHYAMPSUG', 'SAPORTL', 'DAFODILCOM', 'SAMORITA' ]

list = ['IBP', 'SAMORITA', 'JMISMDL', 'SAMATALETH', 'KDSALTD', 'ISLAMIINS', 'QUASEMIND', 'DSHGARME', 'BEACHHATCH', 'NTLTUBES' ]
list = ['JMISMDL', 'SONALIANSH', 'EHL', 'ASIATICLAB', 'SINOBANGLA', 'WATACHEM', 'JHRML', 'LIBRAINFU', 'HAMI', 'SONALIPAPR', 'YPL', 'LHB', 'RANFOUNDRY', 
    'ECABLES', 'APEXFOODS', 'DSHGARME', 'ISNLTD']
list = ['KPCL', 'KBPPWBIL', 'IBP', 'KAY&QUE', 'ASIATICLAB', 'CVOPRL', 'ACFL', 'AGNISYSL', 'AMBEEPHA', 'PHARMAID', 'ZAHEENSPIN', 'SONALIPAPR', 'ACMEPL', 'APEXFOODS', 'MPETROLEUM', 'DACCADYE', 'ATLASBANG', 'FINEFOODS', 'QUASEMIND', 'SIMTEX', 'EGEN', 'CENTRALPHL', 'PADMAOIL', 'SINOBANGLA', 'SAIHAMCOT', 'EHL', 'TITASGAS', 'KDSALTD', 'KOHINOOR', 'BENGALWTL']
list = ['FINEFOODS', 'EHL', 'ASIATICLAB', 'APEXFOODS', 'CVOPRL', 'SONALIPAPR', 'KDSALTD', 'SINOBANGLA', 'SAIHAMCOT', 'SIMTEX', 'AGNISYSL', 'KPCL', 'KBPPWBIL', 'EGEN', 'QUASEMIND']
list = ['ACMEPL','ADNTEL','ADVENT','AGNISYSL','APEXFOODS','APEXSPINN','ASIATICLAB','ATLASBANG','BANGAS','BARKAPOWER','BDAUTOCA','BDCOM','BENGALWTL','CAPMIBBLMF','CVOPRL','DOMINAGE','DSHGARME','DSSL','EASTRNLUB','ECABLES','EGEN','FEKDIL','FINEFOODS','FORTUNE','GBBPOWER','GQBALLPEN','HEIDELBCEM','HFL','HWAWELLTEX','IBP','IFADAUTOS','INTRACO','ISLAMIBANK','ISLAMICFIN','ISLAMIINS','JAMUNAOIL','JHRML','JMISMDL','KBPPWBIL','KDSALTD','KOHINOOR','KPCL','LEGACYFOOT','LHB','LINDEBD','MARICO','MITHUNKNIT','MLDYEING','MONNOAGML','MPETROLEUM','NAHEEACP','NAVANAPHAR','NTLTUBES','OAL','OLYMPIC','PADMAOIL','PHARMAID','RAHIMAFOOD','RANFOUNDRY','RECKITTBEN','SAMATALETH','SHAHJABANK','SILCOPHL','SILVAPHL','SONALIPAPR','SQURPHARMA','TITASGAS','UPGDCL','USMANIAGL','VFSTDL','WALTONHIL','ZAHEENSPIN'
]
list = ['ACMEPL','BDAUTOCA','HFL', 'HWAWELLTEX', 'INTRACO']


'''
[
'IBP',          1162+   11.5
'SAMORITA',     218-    66      Try
'JMISMDL',      300-    116     Try
'SAMATALETH',   103-    80      Try
'KDSALTD',      747=    38
'ISLAMIINS',    411-    36
'QUASEMIND',    799+    37
'DSHGARME',     82+     96
'BEACHHATCH',   414+    30
'NTLTUBES'      348-    57
 ]
'''
#list = ['GPHISPAT', 'MONNOCERA', 'YPL']
print(f"Total items: {len(list)}")
today = date.today()
formatted_date_string = today.strftime("%m%d%Y")
i = 1
for item in list:
    print(f"{i}. {item}")
    i += 1
    response = requests.get(f"https://www.amarstock.com/chart/hover/{item}", stream=True)
    with open(f"Chart/{item}_{formatted_date_string}.gif", "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
    time.sleep(3)  # Sleep for 2 seconds between requests to be polite to the server

    #with open(f"Chart/html.txt", "a") as f:
        #f.write(f"<img src='https://www.amarstock.com/chart/hover/{item}' alt='{item}' />\n")


#response = requests.get("https://www.amarstock.com/chart/hover/MALEKSPIN", stream=True)
#with open("MALEKSPIN.png", "wb") as f:
#    for chunk in response.iter_content(chunk_size=8192):
#        f.write(chunk)
#https://realpython.com/api-integration-in-python/
# https://realpython.com/get-started-with-django-1/
'''
list = "[";
Array.from(document.querySelectorAll("a.scrip")).filter(function(e, i) {
    list += "'" + e.textContent + "', ";
})
list += "]";
console.log(list);

DSE Price change list
list = "list = [";
Array.from(document.querySelectorAll(".fixedHeader")[1].querySelectorAll("tr")).filter(function(e, i) {
    if (Number(e.children[7].textContent.trim()) > 0 &&
        Number(e.children[7].textContent.trim()) < 7 &&
        Number(e.children[2].textContent.trim()) > 9 &&
        Number(e.children[2].textContent.trim()) < 200)
        list += "'" + e.children[1].textContent.trim() + "', ";
})
list += "]";
console.log(list);

DSE TOP 20 Trade list
list = "list = [";
Array.from(document.querySelectorAll(".table.table-bordered.background-white.shares-table")[2].querySelectorAll("tr")).filter(function(e, i) {
    if (e.children[2].textContent.trim() > e.children[5].textContent.trim())
        list += "'" + e.children[1].textContent.trim() + "', ";
})
list += "]";
console.log(list);

DSE Data archive
list = "INSERT INTO `vomule_analysis` (`created_at`, `code`, `ltp`, `high`, `low`, `openp`, `closep`, `ycp`, `trade`, `value`, `volume`) VALUES \n";
Array.from(document.querySelectorAll(".table.table-bordered.background-white.shares-table.fixedHeader")[1].querySelectorAll("tr")).filter(function(e, i) {
    list += "('" + e.children[1].textContent.trim() + "', '" + e.children[2].textContent.trim() + "', " + 
        e.children[3].textContent.trim().replace(/[^0-9.-]+/g, "") + ", " + e.children[4].textContent.trim().replace(/[^0-9.-]+/g, "") + ", " + 
        e.children[5].textContent.trim().replace(/[^0-9.-]+/g, "") + ", " + e.children[6].textContent.trim().replace(/[^0-9.-]+/g, "") + ", " + 
        e.children[7].textContent.trim().replace(/[^0-9.-]+/g, "") + ", " + e.children[8].textContent.trim().replace(/[^0-9.-]+/g, "") + ", " + 
        e.children[9].textContent.trim().replace(/[^0-9.-]+/g, "") + ", " + e.children[10].textContent.trim().replace(/[^0-9.-]+/g, "") + ", " +  
        e.children[11].textContent.trim().replace(/[^0-9.-]+/g, "") + " ),\n";
})
console.log(list);

DSE % change
list = "DELETE FROM vomule_analysis WHERE created_at = CURDATE();\n" +
    "INSERT INTO `vomule_analysis` (`created_at`, `code`, `ltp`, `high`, `low`, `openp`, `closep`, `ycp`, `trade`, `value`, `volume`) VALUES \n";
Array.from(document.querySelectorAll(".table.table-bordered.background-white.shares-table.fixedHeader")[1].querySelectorAll("tr")).filter(function(e, i) {
    list += "(CURDATE(), '" + e.children[1].textContent.trim() + "', " + 
        e.children[2].textContent.trim().replace(/[^0-9.-]+/g, "") + ", " + e.children[3].textContent.trim().replace(/[^0-9.-]+/g, "") + ", " + 
        e.children[4].textContent.trim().replace(/[^0-9.-]+/g, "") + ", 0, 0, " + 
        e.children[6].textContent.trim().replace(/[^0-9.-]+/g, "") + ", " + e.children[8].textContent.trim().replace(/[^0-9.-]+/g, "") + ", " + 
        e.children[9].textContent.trim().replace(/[^0-9.-]+/g, "") + ", " + e.children[10].textContent.trim().replace(/[^0-9.-]+/g, "") + " ),\n";
})
console.log(list);

CREATE TABLE vomule_analysis (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    code VARCHAR(20),
    ltp float,
    high float,
    low float,
    openp float,
    closep float,
    ycp float,
    trade INT,
    value float,
    volume int,
    created_at date
);
SELECT 
    t1.code, t1.created_at yesterday, t2.created_at today,
    t2.volume AS today_sum, 
    t1.volume AS yesterday_sum,
    (t2.volume - t1.volume) * 100 / t1.volume AS increment
FROM 
    vomule_analysis t1
JOIN 
    vomule_analysis t2 ON t1.code = t2.code 
    AND t2.created_at = CURDATE() AND DATE(t1.created_at) = '2026-04-16'
 WHERE 
    t2.value > 10 AND t1.ltp > 10
GROUP BY 
    t1.code
 HAVING 
    today_sum > yesterday_sum
ORDER BY increment DESC;


setInterval(function () {
    document.querySelectorAll(".fa-exchange")[0].click();
    setTimeout(function () {
        document.querySelector("#global_gainloss").textContent = "";
        document.querySelectorAll(".lm_tab[title='Watchlist 1']")[0].click()
    }, 30000);
    setTimeout(function () {
        document.querySelectorAll(".fa-star")[0].click();
    }, 9000);
    /*setTimeout(function () {
        document.getElementById("header-toolbar-symbol-search").click();
    }, 10000);
    setTimeout(function () {
        document.getElementsByClassName("search-RSKUFnp7")[0].value = "DSEX";
        document.getElementsByClassName("search-RSKUFnp7")[0].dispatchEvent(new Event('input', { bubbles: true }));
    }, 12000);
    setTimeout(function () {
        document.getElementsByClassName("itemInfoCell-uhHv1IHJ")[0].click()
    }, 14000);*/
}, 540000)

python -m uvicorn web.app:app --host 127.0.0.1 --port 8000

'''