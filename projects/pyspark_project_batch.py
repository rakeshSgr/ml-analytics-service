# -----------------------------------------------------------------
# Name : pyspark_project_batch.py
# Author :
# Description :
#
# -----------------------------------------------------------------

import json, sys, time
from configparser import ConfigParser,ExtendedInterpolation
from pymongo import MongoClient
from bson.objectid import ObjectId
import os
import requests
from pyspark.sql import SparkSession
from pyspark.sql.functions import *
import pyspark.sql.functions as F
from pyspark.sql.types import *
from pyspark.sql import Row
from collections import OrderedDict, Counter
import databricks.koalas as ks
from azure.storage.blob import BlockBlobService, PublicAccess
from azure.storage.blob import ContentSettings
import logging
import logging.handlers
from logging.handlers import TimedRotatingFileHandler
import datetime
from datetime import date
import redis
from pyspark.sql import DataFrame
from typing import Iterable

config_path = os.path.split(os.path.dirname(os.path.abspath(__file__)))
config = ConfigParser(interpolation=ExtendedInterpolation())
config.read(config_path[0] + "/config.ini")

formatter = logging.Formatter('%(asctime)s - %(levelname)s')

successLogger = logging.getLogger('success log')
successLogger.setLevel(logging.DEBUG)

# Add the log message handler to the logger
successHandler = logging.handlers.RotatingFileHandler(
    config.get('LOGS', 'project_success')
)
successBackuphandler = TimedRotatingFileHandler(
    config.get('LOGS','project_success'),
    when="w0",
    backupCount=1
)
successHandler.setFormatter(formatter)
successLogger.addHandler(successHandler)
successLogger.addHandler(successBackuphandler)

errorLogger = logging.getLogger('error log')
errorLogger.setLevel(logging.ERROR)
errorHandler = logging.handlers.RotatingFileHandler(
    config.get('LOGS', 'project_error')
)
errorBackuphandler = TimedRotatingFileHandler(
    config.get('LOGS', 'project_error'),
    when="w0",
    backupCount=1
)
errorHandler.setFormatter(formatter)
errorLogger.addHandler(errorHandler)
errorLogger.addHandler(errorBackuphandler)

try:
    def convert_to_row(d: dict) -> Row:
        return Row(**OrderedDict(sorted(d.items())))
except Exception as e:
    errorLogger.error(e, exc_info=True)

spark = SparkSession.builder.appName("projects").config("spark.driver.memory", "25g").getOrCreate()

clientProd = MongoClient(config.get('MONGO', 'mongo_url'))
db = clientProd[config.get('MONGO', 'database_name')]
projectsCollec = db[config.get('MONGO', 'projects_collection')]
entitiesCollec = db[config.get('MONGO', 'entities_collection')]

# redis cache connection 
redis_connection = redis.ConnectionPool(
    host=config.get("REDIS", "host"), 
    decode_responses=True, 
    port=config.get("REDIS", "port"), 
    db=config.get("REDIS", "db_name")
)
datastore = redis.StrictRedis(connection_pool=redis_connection)

try:
    def removeduplicate(it):
        seen = []
        for x in it:
            if x not in seen:
                yield x
                seen.append(x)
except Exception as e:
    errorLogger.error(e, exc_info=True)

try:
 def melt(df: DataFrame,id_vars: Iterable[str], value_vars: Iterable[str],
        var_name: str="variable", value_name: str="value") -> DataFrame:

    _vars_and_vals = array(*(
        struct(lit(c).alias(var_name), col(c).alias(value_name))
        for c in value_vars))

    # Add to the DataFrame and explode
    _tmp = df.withColumn("_vars_and_vals", explode(_vars_and_vals))

    cols = id_vars + [
            col("_vars_and_vals")[x].alias(x) for x in [var_name, value_name]]
    return _tmp.select(*cols)
except Exception as e:
   errorLogger.error(e,exc_info=True)
   

spark = SparkSession.builder.appName("projects").config(
    "spark.driver.memory", "50g"
).config(
    "spark.executor.memory", "100g"
).config(
    "spark.memory.offHeap.enabled", True
).config(
    "spark.memory.offHeap.size", "32g"
).getOrCreate()

sc = spark.sparkContext

projects_cursorMongo = projectsCollec.aggregate(
    [{"$match": {"isAPrivateProgram": {"$exists":True,"$ne":None}}},
     {
        "$project": {
            "_id": {"$toString": "$_id"},
            "projectTemplateId": {"$toString": "$projectTemplateId"},
            "solutionInformation": {"name": 1,"_id":{"$toString": "$solutionInformation._id"}},
            "title": 1,
            "programId": {"$toString": "$programId"},
            "programInformation": {"name": 1},
            "metaInformation": {"duration": 1,"goal":1},
            "syncedAt": 1,
            "updatedAt": 1,
            "isDeleted": 1,
            "categories": 1,
            "tasks": 1,
            "status": 1,
            "userId": 1,
            "description": 1,
            "createdAt": 1,
            "programExternalId": 1,
            "isAPrivateProgram": 1,
            "hasAcceptedTAndC": 1,
            "userRoleInformation": 1
        }
    }]
)

projects_schema = StructType([
    StructField('_id', StringType(), True),
    StructField('projectTemplateId', StringType(), True),
    StructField(
        'solutionInformation',
        StructType([StructField('name', StringType(), True),
          StructField('_id', StringType(), True)])
    ),
    StructField('title', StringType(), True),
    StructField('programId', StringType(), True),
    StructField('programExternalId', StringType(), True),
    StructField(
        'programInformation',
        StructType([StructField('name', StringType(), True)])
    ),
    StructField(
        'metaInformation',
        StructType([StructField('duration', StringType(), True),
                    StructField('goal', StringType(), True)
                    ])
    ),
    StructField('updatedAt', TimestampType(), True),
    StructField('syncedAt', TimestampType(), True),
    StructField('isDeleted', BooleanType(), True),
    StructField('status', StringType(), True),
    StructField('userId', StringType(), True),
    StructField('description', StringType(), True),
    StructField('createdAt', TimestampType(), True),
    StructField('isAPrivateProgram', BooleanType(), True),
    StructField('hasAcceptedTAndC', BooleanType(), True),
    StructField(
        'categories',
        ArrayType(
            StructType([StructField('name', StringType(), True)])
        ), True
    ),
    StructField(
          'userRoleInformation',
          StructType([
              StructField('state', StringType(), True),
              StructField('block', StringType(), True),
              StructField('district', StringType(), True),
              StructField('cluster', StringType(), True),
              StructField('school', StringType(), True),
              StructField('role', StringType(), True)
         ])
    ),
    StructField(
        'tasks',
        ArrayType(
            StructType([
                StructField('_id', StringType(), True),
                StructField('name', StringType(), True),
                StructField('assignee', StringType(), True),
                StructField(
                    'attachments',
                    ArrayType(
                        StructType([
                            StructField('sourcePath', StringType(), True)
                        ])
                    )
                ),
                StructField('startDate', StringType(), True),
                StructField('endDate', StringType(), True),
                StructField('syncedAt', TimestampType(), True),
                StructField('status', StringType(), True),
                StructField('isDeleted', BooleanType(), True),
                StructField('remarks',StringType(),True),
                StructField(
                    'children',
                    ArrayType(
                        StructType([
                            StructField('_id', StringType(), True),
                            StructField('name', StringType(), True),
                            StructField('startDate',StringType(), True),
                            StructField('endDate', StringType(), True),
                            StructField('syncedAt', TimestampType(), True),
                            StructField('status', StringType(), True),
                            StructField('isDeleted', BooleanType(), True),
                        ])
                    )
                ),
            ])
        ), True
    )
])

projects_rdd = spark.sparkContext.parallelize(list(projects_cursorMongo))
projects_df = spark.createDataFrame(projects_rdd,projects_schema)

projects_df = projects_df.withColumn(
    "project_created_type",
    F.when(
        projects_df["projectTemplateId"].isNotNull() == True ,
        "project imported from library"
    ).otherwise("user created project")
)

projects_df = projects_df.withColumn(
    "project_title",
    F.when(
        projects_df["solutionInformation"]["name"].isNotNull() == True,
        projects_df["solutionInformation"]["name"]
    ).otherwise(projects_df["title"])
)

projects_df = projects_df.withColumn(
    "project_deleted_flag",
    F.when(
        (projects_df["isDeleted"].isNotNull() == True) & 
        (projects_df["isDeleted"] == True),
        "true"
    ).when(
        (projects_df["isDeleted"].isNotNull() == True) & 
        (projects_df["isDeleted"] == False),
        "false"
    ).otherwise("false")
)

projects_df = projects_df.withColumn(
    "private_program",
    F.when(
        (projects_df["isAPrivateProgram"].isNotNull() == True) & 
        (projects_df["isAPrivateProgram"] == True),
        "true"
    ).when(
        (projects_df["isAPrivateProgram"].isNotNull() == True) & 
        (projects_df["isAPrivateProgram"] == False),
        "false"
    ).otherwise("true")
)

projects_df = projects_df.withColumn(
    "project_terms_and_condition",
    F.when(
        (projects_df["hasAcceptedTAndC"].isNotNull() == True) & 
        (projects_df["hasAcceptedTAndC"] == True),
        "true"
    ).when(
        (projects_df["hasAcceptedTAndC"].isNotNull() == True) & 
        (projects_df["hasAcceptedTAndC"] == False),
        "false"
    ).otherwise("false")
)

projects_df = projects_df.withColumn(
    "project_completed_date",
    F.when(
        projects_df["status"] == "completed",
        projects_df["updatedAt"]
    ).otherwise(None)
)

projects_df = projects_df.withColumn(
    "exploded_categories", F.explode_outer(F.col("categories"))
)

projects_df = projects_df.withColumn("parent_channel", F.lit("SHIKSHALOKAM"))

projects_df = projects_df.withColumn("exploded_tasks", F.explode_outer(F.col("tasks")))

projects_df = projects_df.withColumn(
    "exploded_tasks_attachments",
    F.explode_outer(projects_df["exploded_tasks"]["attachments"])
)

projects_df = projects_df.withColumn(
    "task_evidence_status", 
    F.when(
        projects_df["exploded_tasks_attachments"]["sourcePath"].isNotNull() == True,
        True
    ).otherwise(False)
)

projects_df = projects_df.withColumn(
    "task_deleted_flag",
    F.when(
        (projects_df["exploded_tasks"]["isDeleted"].isNotNull() == True) & 
        (projects_df["exploded_tasks"]["isDeleted"] == True),
        "true"
    ).when(
        (projects_df["exploded_tasks"]["isDeleted"].isNotNull() == True) & 
        (projects_df["exploded_tasks"]["isDeleted"] == False),
        "false"
    ).otherwise("false")
)

projects_df = projects_df.withColumn(
    "task_evidence",
    F.when(
        projects_df["exploded_tasks_attachments"]["sourcePath"].isNotNull() == True,
        F.concat(
            F.lit(config.get('ML_SURVEY_SERVICE_URL', 'evidence_base_url')),
            projects_df["exploded_tasks_attachments"]["sourcePath"]
        )
    )
)

projects_df = projects_df.withColumn(
    "exploded_sub_tasks", F.explode_outer(projects_df["exploded_tasks"]["children"])
)

projects_df = projects_df.withColumn(
    "sub_task_deleted_flag",
    F.when((
        projects_df["exploded_sub_tasks"]["isDeleted"].isNotNull() == True) & 
        (projects_df["exploded_sub_tasks"]["isDeleted"] == True),
        "true"
    ).when(
        (projects_df["exploded_sub_tasks"]["isDeleted"].isNotNull() == True) & 
        (projects_df["exploded_sub_tasks"]["isDeleted"] == False),
        "false"
    ).otherwise("false")
)

projects_df_cols = projects_df.select(
    projects_df["_id"].alias("project_id"),
    projects_df["project_created_type"],
    projects_df["project_title"],
    projects_df["title"].alias("project_title_editable"),
    projects_df["programId"].alias("program_id"),
    projects_df["programExternalId"].alias("program_externalId"),
    projects_df["programInformation"]["name"].alias("program_name"),
    projects_df["metaInformation"]["duration"].alias("project_duration"),
    projects_df["syncedAt"].alias("project_last_sync"),
    projects_df["updatedAt"].alias("project_updated_date"),
    projects_df["project_deleted_flag"],
    projects_df["exploded_categories"]["name"].alias("area_of_improvement"),
    projects_df["status"].alias("status_of_project"),
    projects_df["userId"].alias("createdBy"),
    projects_df["description"].alias("project_description"),
    projects_df["metaInformation"]["goal"].alias("project_goal"),
    projects_df["parent_channel"],
    projects_df["createdAt"].alias("project_created_date"),
    projects_df["exploded_tasks"]["_id"].alias("task_id"),
    projects_df["exploded_tasks"]["name"].alias("tasks"),
    projects_df["exploded_tasks"]["assignee"].alias("task_assigned_to"),
    projects_df["exploded_tasks"]["startDate"].alias("task_start_date"),
    projects_df["exploded_tasks"]["endDate"].alias("task_end_date"),
    projects_df["exploded_tasks"]["syncedAt"].alias("tasks_date"),projects_df["exploded_tasks"]["status"].alias("tasks_status"),
    projects_df["task_evidence"],
    projects_df["task_evidence_status"],
    projects_df["exploded_sub_tasks"]["_id"].alias("sub_task_id"),
    projects_df["exploded_sub_tasks"]["name"].alias("sub_task"),
    projects_df["exploded_sub_tasks"]["status"].alias("sub_task_status"),
    projects_df["exploded_sub_tasks"]["syncedAt"].alias("sub_task_date"),
    projects_df["exploded_sub_tasks"]["startDate"].alias("sub_task_start_date"),
    projects_df["exploded_sub_tasks"]["endDate"].alias("sub_task_end_date"),
    projects_df["private_program"],
    projects_df["task_deleted_flag"],
    projects_df["sub_task_deleted_flag"],
    projects_df["project_terms_and_condition"],
    projects_df["exploded_tasks"]["remarks"].alias("task_remarks"),
    projects_df["project_completed_date"],
    projects_df["solutionInformation"]["_id"].alias("solution_id"),
    projects_df["userRoleInformation"]["role"].alias("designation"),
    projects_df["userRoleInformation"]["state"].alias("state_externalId"),
    projects_df["userRoleInformation"]["block"].alias("block_externalId"),
    projects_df["userRoleInformation"]["district"].alias("district_externalId"),
    projects_df["userRoleInformation"]["cluster"].alias("cluster_externalId"),
    projects_df["userRoleInformation"]["school"].alias("school_externalId")
)

projects_df_cols = projects_df_cols.dropDuplicates()

projects_userid_df = projects_df_cols.select("createdBy")

projects_entities_id_df = projects_df_cols.select("state_externalId","block_externalId","district_externalId","cluster_externalId","school_externalId")
entitiesId_projects_df_before = []
entitiesId_arr = []
uniqueEntitiesId_arr = []
entitiesId_projects_df_before = projects_entities_id_df.toJSON().map(lambda j: json.loads(j)).collect()
for eid in entitiesId_projects_df_before:
   try:
    entitiesId_arr.append(eid["state_externalId"])
   except KeyError :
    pass
   try:
    entitiesId_arr.append(eid["block_externalId"])
   except KeyError :
    pass
   try:
    entitiesId_arr.append(eid["district_externalId"])
   except KeyError :
    pass
   try:
    entitiesId_arr.append(eid["cluster_externalId"])
   except KeyError :
    pass
   try:
    entitiesId_arr.append(eid["school_externalId"])
   except KeyError :
    pass
uniqueEntitiesId_arr = list(removeduplicate(entitiesId_arr))
ent_cursorMongo = entitiesCollec.aggregate(
   [{"$match": {"$or":[{"registryDetails.locationId":{"$in":uniqueEntitiesId_arr}},{"registryDetails.code":{"$in":uniqueEntitiesId_arr}}]}},
    {
      "$project": {
         "_id": {"$toString": "$_id"},
         "entityType": 1,
         "metaInformation": {"name": 1},
         "registryDetails": 1
      }
    }
   ])
ent_schema = StructType(
        [
            StructField("_id", StringType(), True),
            StructField("entityType", StringType(), True),
            StructField("metaInformation",
                StructType([StructField('name', StringType(), True)])
            ),
            StructField("registryDetails",
                StructType([StructField('locationId', StringType(), True),
                            StructField('code',StringType(), True)
                        ])
            )
        ]
    )
entities_rdd = spark.sparkContext.parallelize(list(ent_cursorMongo))
entities_df = spark.createDataFrame(entities_rdd,ent_schema)
entities_df = melt(entities_df,
        id_vars=["_id","entityType","metaInformation.name"],
        value_vars=["registryDetails.locationId", "registryDetails.code"]
    ).select("_id","entityType","name","value"
            ).dropDuplicates()
entities_df = entities_df.withColumn("variable",F.concat(F.col("entityType"),F.lit("_externalId")))
projects_df_melt = melt(projects_df_cols,
        id_vars=["project_id", "project_created_type", "project_title", "project_title_editable", "program_id", "program_externalId", "program_name", "project_duration", "project_last_sync", "project_updated_date", "project_deleted_flag", "area_of_improvement", "status_of_project", "createdBy", "project_description", "project_goal", "parent_channel", "project_created_date", "task_id", "tasks", "task_assigned_to", "task_start_date", "task_end_date", "tasks_date", "tasks_status", "task_evidence", "task_evidence_status", "sub_task_id", "sub_task", "sub_task_status", "sub_task_date", "sub_task_start_date", "sub_task_end_date", "private_program", "task_deleted_flag", "sub_task_deleted_flag", "project_terms_and_condition", "task_remarks", "project_completed_date", "solution_id", "designation"],
        value_vars=["state_externalId", "block_externalId", "district_externalId", "cluster_externalId", "school_externalId"]
        )
projects_ent_df_melt = projects_df_melt\
                 .join(entities_df,["variable","value"],how="left")\
                 .select(projects_df_melt["*"],entities_df["name"],entities_df["_id"].alias("entity_ids"))
projects_ent_df_melt = projects_ent_df_melt.withColumn("flag",F.regexp_replace(F.col("variable"),"_externalId","_name"))
projects_ent_df_melt = projects_ent_df_melt.groupBy(["project_id"])\
                               .pivot("flag").agg(first(F.col("name")))
projects_df_final = projects_df_cols.join(projects_ent_df_melt,["project_id"],how="left")

userId_projects_df_before = []
userId_projects_df_after = []
userId_arr = []
uniqueuserId_arr = []
userId_projects_df_before = projects_userid_df.toJSON().map(lambda j: json.loads(j)).collect()
for uid in userId_projects_df_before:
    userId_arr.append(uid["createdBy"])

uniqueuserId_arr = list(removeduplicate(userId_arr))

user_info_arr = []

for usr in uniqueuserId_arr:
    userObj = {}
    userObj = datastore.hgetall("user:"+usr)
    if userObj :
        rootOrgId = None
        boardName = None
        try:
            rootOrgId = userObj["rootorgid"]
        except KeyError :
            rootOrgId = ''
        try:
         boardName = userObj["board"]
        except KeyError:
         boardName = ''

        userInfoObj = {}
        userInfoObj["board_name"] = boardName
        userInfoObj["id"] = usr
        userInfoObj["channel"] = rootOrgId
        try:
            userInfoObj["organisation_name"] = userObj["orgname"]
        except KeyError:
            userInfoObj["organisation_name"] = ''
        user_info_arr.append(userInfoObj)

user_df = ks.DataFrame(user_info_arr)
user_df = user_df.to_spark()

final_projects_df = projects_df_final.join(
    user_df,
    projects_df_final["createdBy"] == user_df["id"],
    "inner"
).drop(user_df["id"])

final_projects_df = final_projects_df.dropDuplicates()

final_projects_df.coalesce(1).write.format("json").mode("overwrite").save(
    config.get("OUTPUT_DIR", "project") + "/"
)

for filename in os.listdir(config.get("OUTPUT_DIR", "project")+"/"):
    if filename.endswith(".json"):
       os.rename(
           config.get("OUTPUT_DIR", "project") + "/" + filename,
           config.get("OUTPUT_DIR", "project") + "/sl_projects.json"
        )

blob_service_client = BlockBlobService(
    account_name=config.get("AZURE", "account_name"), 
    sas_token=config.get("AZURE", "sas_token")
)
container_name = config.get("AZURE", "container_name")
local_path = config.get("OUTPUT_DIR", "project")
blob_path = config.get("AZURE", "projects_blob_path")

for files in os.listdir(local_path):
    if "sl_projects.json" in files:
        blob_service_client.create_blob_from_path(
            container_name,
            os.path.join(blob_path,files),
            local_path + "/" + files
        )

os.remove(config.get("OUTPUT_DIR", "project") + "/sl_projects.json")

dimensionsArr = []
entitiesArr = ["state_externalId", "block_externalId", "district_externalId", "cluster_externalId", "school_externalId",\
              "state_name","block_name","district_name","cluster_name","school_name","board_name"]
dimensionsArr = list(set(entitiesArr))

submissionReportColumnNamesArr = [
    'project_title', 'project_goal', 'project_created_date', 'project_last_sync',
    'area_of_improvement', 'status_of_project', 'tasks', 'tasks_date', 'tasks_status',
    'sub_task', 'sub_task_status', 'sub_task_date', 'task_start_date', 'task_end_date',
    'sub_task_start_date', 'sub_task_end_date', 'designation', 'project_deleted_flag',
    'task_evidence', 'task_evidence_status', 'project_id', 'task_id', 'sub_task_id',
    'project_created_type', 'task_assigned_to', 'channel', 'parent_channel', 'program_id',
    'program_name', 'project_updated_date', 'createdBy', 'project_title_editable', 
    'project_duration', 'program_externalId', 'private_program', 'task_deleted_flag',
    'sub_task_deleted_flag', 'project_terms_and_condition','task_remarks',
    'organisation_name','project_description','project_completed_date','solution_id'
]

dimensionsArr.extend(submissionReportColumnNamesArr)

payload = {}
payload = json.loads(config.get("DRUID","project_injestion_spec"))
payload["spec"]["dataSchema"]["dimensionsSpec"]["dimensions"] = dimensionsArr
datasources = [payload["spec"]["dataSchema"]["dataSource"]]
ingestion_specs = [json.dumps(payload)]

for i, j in zip(datasources,ingestion_specs):
    druid_end_point = config.get("DRUID", "metadata_url") + i
    druid_batch_end_point = config.get("DRUID", "batch_url")
    headers = {'Content-Type' : 'application/json'}
    get_timestamp = requests.get(druid_end_point, headers=headers)
    if get_timestamp.status_code == 200:
        successLogger.debug("Successfully fetched time stamp of the datasource " + i )
        timestamp = get_timestamp.json()
        #calculating interval from druid get api
        minTime = timestamp["segments"]["minTime"]
        maxTime = timestamp["segments"]["maxTime"]
        min1 = datetime.datetime.strptime(minTime, "%Y-%m-%dT%H:%M:%S.%fZ")
        max1 = datetime.datetime.strptime(maxTime, "%Y-%m-%dT%H:%M:%S.%fZ")
        new_format = "%Y-%m-%d"
        min1.strftime(new_format)
        max1.strftime(new_format)
        minmonth = "{:02d}".format(min1.month)
        maxmonth = "{:02d}".format(max1.month)
        min2 = str(min1.year) + "-" + minmonth + "-" + str(min1.day)
        max2 = str(max1.year) + "-" + maxmonth  + "-" + str(max1.day)
        interval = min2 + "_" + max2
        time.sleep(50)

        disable_datasource = requests.delete(druid_end_point, headers=headers)

        if disable_datasource.status_code == 200:
            successLogger.debug("successfully disabled the datasource " + i)
            time.sleep(300)
          
            delete_segments = requests.delete(
                druid_end_point + "/intervals/" + interval, headers=headers
            )
            if delete_segments.status_code == 200:
                successLogger.debug("successfully deleted the segments " + i)
                time.sleep(300)

                enable_datasource = requests.get(druid_end_point, headers=headers)
                if enable_datasource.status_code == 204:
                    successLogger.debug("successfully enabled the datasource " + i)
                    
                    time.sleep(300)

                    start_supervisor = requests.post(
                        druid_batch_end_point, data=j, headers=headers
                    )
                    successLogger.debug("ingest data")
                    if start_supervisor.status_code == 200:
                        successLogger.debug(
                            "started the batch ingestion task sucessfully for the datasource " + i
                        )
                        time.sleep(50)
                    else:
                        errorLogger.error(
                            "failed to start batch ingestion task" + str(start_supervisor.status_code)
                        )
                else:
                    errorLogger.error("failed to enable the datasource " + i)
            else:
                errorLogger.error("failed to delete the segments of the datasource " + i)
        else:
            errorLogger.error("failed to disable the datasource " + i)

    elif get_timestamp.status_code == 204:
        start_supervisor = requests.post(
            druid_batch_end_point, data=j, headers=headers
        )
        if start_supervisor.status_code == 200:
            successLogger.debug(
                "started the batch ingestion task sucessfully for the datasource " + i
            )
            time.sleep(50)
        else:
            errorLogger.error(start_supervisor.text)
            errorLogger.error(
                "failed to start batch ingestion task" + str(start_supervisor.status_code)
            )

