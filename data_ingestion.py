import mysql.connector
import pandas as pd

df=pd.read_csv('processed_data.csv')


conn=mysql.connector.connect(
    host='localhost',
    user='root',
    password='Root',
    database='itinerary'
)

cursor=conn.cursor()

for _,row in df.iterrows():
    sql="INSERT INTO trips(place,rating,review_count,image_url,page_link,place_desc,duration,area,state,city_region) values(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    row = row.where(pd.notnull(row), None)
    values=tuple(row)
    cursor.execute(sql,values)

conn.commit()
conn.close()

print('data inserted..........')