'''
Created on 27 Aug 2012

@author: elav01
'''

#In case the scoring functions of a suite experiment have been updated, but all the previous steps are ok
#this routine parses all subfolders and recalculates every pairwise-based score. 

import os
import fnmatch
import logging
import sys
import subprocess

FORMAT = "%(asctime)-15s [%(process)d:%(thread)d] %(message)s "

logging.basicConfig(filename='rerun.log',level=logging.DEBUG, format=FORMAT)

finisheddirectories = set()

rerun_n = sys.argv[1]

#first construct a set with all the relevant directory paths
#which contain the required "reconstructed" jcml files
for root, dirnames, filenames in os.walk('.'):
    for filename in fnmatch.filter(filenames, 'experiment.cfg'):
        fullfilename = os.path.join(root, filename)
        subprocess.call("python2.7 ~/workspace/TaraXUscripts/src/experiment/autoranking/suite.py -c {} -e {} -r {}".format(fullfilename, root, rerun_n), shell=True)
        
        
    