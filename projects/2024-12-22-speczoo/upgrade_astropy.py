from astropy.time import Time
from astropy.coordinates import EarthLocation

# Downloading https://datacenter.iers.org/data/9/finals2000A.all
# 创建 UTC 时间对象
t_utc = Time("2024-01-01 00:00:00", scale="utc")
# 转换为 UT1 时间（触发下载）
t_ut1 = t_utc.ut1

# Downloading http://data.astropy.org/coordinates/sites.json
# 获取所有预定义站点名称
sites = EarthLocation.get_site_names()
