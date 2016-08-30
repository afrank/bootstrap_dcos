#!/usr/bin/python

#
# this script creates a multi-AZ DC/OS cluster in an AWS region.
# 

import boto.ec2
import boto.ec2.elb
import time
import os
import json
import requests

config_file = "config.json"
state_file = "state.json"

CONFIG = json.loads(open(config_file).read())
ZONE_LIST = CONFIG["common"]["ZONES"].keys()

conn = boto.ec2.connect_to_region(CONFIG["common"]["REGION"],
    aws_access_key_id=CONFIG["common"]["AWS_ACCESS_KEY_ID"],
    aws_secret_access_key=CONFIG["common"]["AWS_SECRET_ACCESS_KEY"])

def create_instance(hostname,user_data,CFG,zone):
    print hostname
    # test for an existing instance
    instances = [ r.instances[0] for r in conn.get_all_instances(filters={'tag:Name':hostname,'instance-state-name':'running'}) ]
    subnet_id = CONFIG["common"]["ZONES"][zone]["subnet"]
    if len(instances) > 0:
        print "%s already exists, moving on." % hostname
        return instances[0]
    # the base image size is 5GB, so you can't go smaller than that.
    if "VOL_SIZE" in CFG and CFG["VOL_SIZE"] > 5:
	vol_count = 1
	if "VOL_COUNT" in CFG:
	    vol_count = int(CFG["VOL_COUNT"])
	vols = []
	for c in range(vol_count):
	    vol = boto.ec2.blockdevicemapping.EBSBlockDeviceType()
	    vol.size = CFG["VOL_SIZE"]
	    dev_name = "/dev/xvd{}".format(unichr(97+c))
	    vols += [(dev_name,vol)]
        block_map = boto.ec2.blockdevicemapping.BlockDeviceMapping()
	for b,v in vols:
	    block_map[b] = v
        reservation = conn.run_instances(CFG["AMI_ID"],
            security_group_ids = CONFIG["common"]["SECURITY_GROUPS"],
            user_data = user_data,
            instance_type = CFG["INSTANCE_TYPE"],
            placement = zone,
            key_name = CONFIG["common"]["KEY_NAME"],
            subnet_id = subnet_id,
            block_device_map = block_map
        )
    else:
        print "Attempting to create host %s in zone %s subnet %s" % (hostname,zone,subnet_id)
        reservation = conn.run_instances(CFG["AMI_ID"],
            security_group_ids = CONFIG["common"]["SECURITY_GROUPS"],
            user_data = user_data,
            instance_type = CFG["INSTANCE_TYPE"],
            placement = zone,
            key_name = CONFIG["common"]["KEY_NAME"],
            subnet_id = subnet_id
        )
    instance = reservation.instances[0]
    return instance

def wait_for_ready(instance):
    status = instance.update()
    _id = instance.id
    while status == 'pending':
        time.sleep(3)
        print "%s => %s" % (_id,status)
        status = instance.update()
    ip_address = instance.private_ip_address
    print "%s (%s) => %s" % (_id,ip_address,status)
    while os.system("ping -t3 -c1 %s >/dev/null 2>&1" % ip_address) != 0:
        print "[%s] checking for ping..." % ip_address
        time.sleep(3)
    print "[%s] pingable!" % ip_address
    return instance

new_instances = []
ips = []
zone_pos = 0
state = []

# Set the Etcd Discovery URL
etcd_discovery_url = requests.get("https://discovery.etcd.io/new").text
print "Found Etcd Discovery URL: %s" % etcd_discovery_url

# Provision agents and masters
for ROLE in ('agent','master'):
    user_data_tmpl = open(CONFIG[ROLE]["CLOUD_INIT_TMPL"]).read()
    for i in range(CONFIG[ROLE]["COUNT"]):
        hostname = CONFIG["common"]["HOSTNAME_TMPL"] % { 'num': str(i+1), 'env': CONFIG["common"]["ENV"], 'role': ROLE }
        zone = ZONE_LIST[zone_pos]
	user_data = user_data_tmpl % {'hostname':hostname, 'role': ROLE, 'zone': zone, 'etcd_discovery_url': etcd_discovery_url}
        instance = create_instance(hostname,user_data,CONFIG[ROLE],zone)
        new_instances += [ (hostname,instance,ROLE) ]
	zone_pos = zone_pos + 1 if zone_pos < len(ZONE_LIST)-1 else 0

# wait for masters and agents to be ready
for h,i,r in new_instances:
    instance = wait_for_ready(i)
    ips += [ (r,instance.private_ip_address) ]
    print "[%s] %s (%s) => %s" % (h,instance.id,instance.private_ip_address,instance.update())
    conn.create_tags([instance.id],{'Name':h, 'cf_role':r})
    state += [(h,instance.id,instance.private_ip_address)]

# even though the hosts have been confirmed pingable, in the early life of an AWS instance the network can be somewhat
# flakey for a bit, so just wait a while for them to get through their adolescence. If the bootstrap process can't reach a
# master, the whole thing basically needs to be scrapped and redone.
print "Taking a short nap. Maybe AWS will suck less when I wake up..."
time.sleep(30)

# Provision Bootstrap Node
ROLE='bootstrap'
user_data_tmpl = open(CONFIG[ROLE]["CLOUD_INIT_TMPL"]).read()
master_list = "\n".join(["      - {}".format(x) for r,x in ips if r == 'master'])
agent_list =  "\n".join(["      - {}".format(x) for r,x in ips if r == 'agent'])
for i in range(CONFIG[ROLE]["COUNT"]):
    hostname = CONFIG["common"]["HOSTNAME_TMPL"] % { 'role': ROLE, 'num': str(i+1), 'env': CONFIG["common"]["ENV"] }
    user_data = user_data_tmpl % {'hostname':hostname, 'role': ROLE, 'master_list': master_list, 'agent_list': agent_list }
    zone = ZONE_LIST[zone_pos]
    instance = create_instance(hostname,user_data,CONFIG[ROLE],zone)
    time.sleep(10) # adding this sleep since ocassionally I get an instance-not-found when I start polling too quickly.
    instance = wait_for_ready(instance)
    print "[%s] %s (%s) => %s" % (hostname, instance.id,instance.private_ip_address,instance.update())
    conn.create_tags([instance.id],{'Name':hostname, 'cf_role':ROLE})
    state += [(hostname,instance.id,instance.private_ip_address)]
    zone_pos = zone_pos + 1 if zone_pos < len(ZONE_LIST)-1 else 0

f = open(state_file,"w")
f.write(json.dumps(state))
f.close()
