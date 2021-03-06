#############################################################################
#
# PyExperimentSuite
#
# Derive your experiment from the PyExperimentSuite, fill in the reset() and
# iterate() methods, and define your defaults and experiments variables
# in a config file.
# PyExperimentSuite will create directories, run the experiments and store the 
# logged data. An aborted experiment can be resumed at any time. If you want
# to resume it on iteration level (instead of repetition level) you need to
# implement the restore_state and save_state method and make sure the 
# restore_supported variable is set to True.
#
# For more information, consult the included documentation.pdf file.
#
# Licensed under the modified BSD License. See LICENSE file in same folder.
#
# Copyright 2010 - Thomas Rueckstiess
#
#############################################################################

from ConfigParser import ConfigParser
from multiprocessing import Process, Pool, cpu_count
from numpy import *
import traceback
import sys
import shutil
import logging
import os, sys, time, itertools, re, optparse, types
from datetime import datetime
import fnmatch
from collections import OrderedDict

def mp_runrep(args):
    """ Helper function to allow multiprocessing support. """
    return PyExperimentSuite.run_rep(*args)

def progress(params, rep):
    """ Helper function to calculate the progress made on one experiment. """
    name = params['name']
    fullpath = os.path.join(params['path'], params['name'])
    logname = os.path.join(fullpath, '%i.log'%rep)
    if os.path.exists(logname):
        logfile = open(logname, 'r')
        lines = logfile.readlines()
        logfile.close()
        return int(100 * len(lines) / params['iterations'])
    else: 
        return 0

def convert_param_to_dirname(param):
    """ Helper function to convert a parameter value to a valid directory name. """
    if type(param) == types.StringType:
        return param
    else:
        return re.sub("0+$", '0', '%f'%param)


class PyExperimentSuite(object):
    
    # change this in subclass, if you support restoring state on iteration level
    restore_supported = False
    
    def __init__(self):
        self.parse_opt()
        
        #don't load the configuration file
        if self.options.rerun_recursive:
            self.rerun_recursive()
            raise SystemExit
        
        self.parse_cfg()
        
        # list of keys, that had to be renamed because they contained spaces
        self.key_warning_issued = []
    
    def parse_opt(self):
        """ parses the command line options for different settings. """
        optparser = optparse.OptionParser()
        optparser.add_option('-c', '--config',
            action='store', dest='config', type='string', default='experiments.cfg', 
            help="your experiments config file")
        optparser.add_option('-n', '--numcores',
            action='store', dest='ncores', type='int', default=cpu_count(), 
            help="number of processes you want to use, default is %i"%cpu_count())  
        optparser.add_option('-d', '--del',
            action='store_true', dest='delete', default=False, 
            help="delete experiment folder if it exists")
        optparser.add_option('-e', '--experiment',
            action='append', dest='experiments', type='string',
            help="run only selected experiments, by default run all experiments in config file.")
        optparser.add_option('-b', '--browse',
            action='store_true', dest='browse', default=False, 
            help="browse existing experiments.")      
        optparser.add_option('-B', '--Browse',
            action='store_true', dest='browse_big', default=False, 
            help="browse existing experiments, more verbose than -b")      
        optparser.add_option('-p', '--progress',
            action='store_true', dest='progress', default=False, 
            help="like browse, but only shows name and progress bar")
        optparser.add_option('-r', '--rerun',
            action='store', dest='rerun', type='int', default=None, 
            help="this allows you to rerun an experiment by specifying the iteration after which everything will be re-executed" )  
        optparser.add_option('-R', '--rerun-recursive',
            action='store', dest='rerun_recursive', type='int', default=None, 
            help="this allows you to rerun many nested experiments by specifying the iteration after which everything will be re-executed" )  
        optparser.add_option('--debug', 
            action='store_true', dest="debug", default=False,
            help="Show additional debugging runtime messages")
        options, args = optparser.parse_args()
        self.options = options
        return options, args
    
    def parse_cfg(self):
        """ parses the given config file for experiments. """
        self.cfgparser = ConfigParser()
        if not self.cfgparser.read(self.options.config):
            raise IOError('config file %s not found.'%self.options.config) 
            
    
    def mkdir(self, path):
        """ create a directory if it does not exist. """
        if not os.path.exists(path):
            os.makedirs(path)
            
    def get_exps(self, path='.'):
        """ go through all subdirectories starting at path and return the experiment
            identifiers (= directory names) of all existing experiments. A directory
            is considered an experiment if it contains a experiment.cfg file. 
        """
        exps = []
        for dp, dn, fn in os.walk(path):
            if 'experiment.cfg' in fn:
                subdirs = [os.path.join(dp, d) for d in os.listdir(dp) if os.path.isdir(os.path.join(dp, d))]
                if all(map(lambda s: self.get_exps(s) == [], subdirs)):       
                    exps.append(dp)
        return exps
    
    def items_to_params(self, items):
        """ evaluate the found items (strings) to become floats, ints or lists. 
        """
        params = {}
        for t,v in items:       
            try:
                # try to evaluate parameter (float, int, list)
                if v in ['grid', 'list']:
                    params[t] = v
                else:
                    params[t] = eval(v)
                if isinstance(params[t], ndarray):
                    params[t] = params[t].tolist()
            except (NameError, SyntaxError):
                # otherwise assume string
                params[t] = v
        return params        
           
    def get_params(self, exp, cfgname='experiment.cfg'):
        """ reads the parameters of the experiment (= path) given.
        """
        cfgp = ConfigParser()
        cfgp.read(os.path.join(exp, cfgname))
        section = cfgp.sections()[0]
        params = self.items_to_params(cfgp.items(section))
        params['name'] = section
        return params

    def get_exp(self, name, path='.'):
        """ given an experiment name (used in section titles), this function
            returns the correct path of the experiment. 
        """
        exps = []
        for dp, dn, df in os.walk(path):
            if 'experiment.cfg' in df:
                cfgp = ConfigParser()
                cfgp.read(os.path.join(dp, 'experiment.cfg'))
                if name in cfgp.sections():
                    exps.append(dp)
        return exps
            
    
    def write_config_file(self, params, path):
        """ write a config file for this single exp in the folder path.
        """
        cfgp = ConfigParser()
        cfgp.add_section(params['name'])
        for p in params:
            if p == 'name':
                continue
            value = params[p]
            #include quotes if needed, to avoid problems on next use of eval
            if isinstance(value, basestring):
                value = '"{}"'.format(value)
            cfgp.set(params['name'], p, value)
        f = open(os.path.join(path, 'experiment.cfg'), 'w')
        cfgp.write(f)
        f.close()
                
    def get_history(self, exp, rep, tags):
        """ returns the whole history for one experiment and one repetition.
            tags can be a string or a list of strings. if tags is a string,
            the history is returned as list of values, if tags is a list of 
            strings or 'all', history is returned as a dictionary of lists
            of values.
        """
        params = self.get_params(exp)
           
        if params == None:
            raise SystemExit('experiment %s not found.'%exp)         
        
        # make list of tags, even if it is only one
        if tags != 'all' and not hasattr(tags, '__iter__'):
            tags = [tags] 
        
        results = {}
        logfile = os.path.join(exp, '%i.log'%rep)
        try:
            f = open(logfile)
        except IOError:
            if len(tags) == 1:
                return []
            else:
                return {}

        for line in f:
            pairs = line.split()
            logging.debug("exp:{} rep:{} tags:{} pairs:{}".format(exp, rep, tags, len(pairs)))
            for pair in pairs:
                try:
                    tag,val = pair.split(':')
                except:
                    logging.warning("Exp: {} rep: {} Result pair not in the required format".format(exp, rep))
                    continue
		 
                if tags == 'all' or tag in tags:
                    if not tag in results:
                        try:
                            results[tag] = [eval(val)]
                        except (NameError, SyntaxError):
                            results[tag] = [val]
                    else:
                        try:
                            results[tag].append(eval(val))
                        except (NameError, SyntaxError):
                            results[tag].append(val)
                            
        f.close()
        logging.debug("results:{}".format(results))
        if len(results) == 0:
            if len(tags) == 1:
                return []
            else:
                return {}
            # raise ValueError('tag(s) not found: %s'%str(tags))
        if len(tags) == 1:
            return results[results.keys()[0]]
        else:
            return results
    
    
    def get_history_tags(self, exp, rep=0):
        """ returns all available tags (logging keys) of the given experiment 
            repetition. 
            
            Note: Technically, each repetition could have different
            tags, therefore the rep number can be passed in as parameter, 
            even though usually all repetitions have the same tags. The default 
            repetition is 0 and in most cases, can be omitted.
        """
        history = self.get_history(exp, rep, 'all')
        return history.keys()
    
    
    def get_value(self, exp, rep, tags, which='last'):
        """ Like get_history(..) but returns only one single value rather
            than the whole list. 
            tags can be a string or a list of strings. if tags is a string,
            the history is returned as a single value, if tags is a list of 
            strings, history is returned as a dictionary of values.
            'which' can be one of the following:
                last: returns the last value of the history
                 min: returns the minimum value of the history
                 max: returns the maximum value of the history
                   #: (int) returns the value at that index
        """
        history = self.get_history(exp, rep, tags)
        
        # empty histories always return None
        if len(history) == 0:
            return None
            
        # distinguish dictionary (several tags) from list
        if type(history) == dict:
            for h in history:
                if which == 'last':
                    history[h] = history[h][-1]
                if which == 'min':
                    history[h] = min(history[h])
                if which == 'max':
                    history[h] = max(history[h])
                if type(which) == int:
                    history[h] = history[h][which]
            return history
            
        else:
            if which == 'last':
                return history[-1]
            if which == 'min':
                return min(history)
            if which == 'max':
                return max(history)
            if type(which) == int:
                return history[which]
            else: 
                return None
        
    def get_values_fix_params(self, exp, rep, tag, which='last', **kwargs):
        """ this function uses get_value(..) but returns all values where the
            subexperiments match the additional kwargs arguments. if alpha=1.0,
            beta=0.01 is given, then only those experiment values are returned,
            as a list.
        """ 
        subexps = self.get_exps(exp)
        tagvalues = ['%s%s'%(k, convert_param_to_dirname(kwargs[k])) for k in kwargs]
        
        values = [self.get_value(se, rep, tag, which) for se in subexps if all(map(lambda tv: tv in se, tagvalues))]
        params = [self.get_params(se) for se in subexps if all(map(lambda tv: tv in se, tagvalues))]
        
        return values, params

    def get_histories_fix_params(self, exp, rep, tag, **kwargs):
        """ this function uses get_history(..) but returns all histories where the
            subexperiments match the additional kwargs arguments. if alpha=1.0,
            beta = 0.01 is given, then only those experiment histories are returned,
            as a list.
        """ 
        subexps = self.get_exps(exp)
        tagvalues = [re.sub("0+$", '0', '%s%f'%(k, kwargs[k])) for k in kwargs]

        histories = [self.get_history(se, rep, tag) for se in subexps if all(map(lambda tv: tv in se, tagvalues))]
        params = [self.get_params(se) for se in subexps if all(map(lambda tv: tv in se, tagvalues))]

        return histories, params
    
    def get_histories_over_repetitions(self, exp, tags, aggregate):
        """ this function gets all histories of all repetitions using get_history() on the given
            tag(s), and then applies the function given by 'aggregate' to all corresponding values
            in each history over all iterations. Typical aggregate functions could be 'mean' or
            'max'.
        """
        params = self.get_params(exp)
        
        # explicitly make tags list in case of 'all'
        if tags == 'all':
            tags = self.get_history(exp, 0, 'all').keys()
        
        # make list of tags if it is just a string
        if not hasattr(tags, '__iter__'):
            tags = [tags]
         
        results = OrderedDict()
        for tag in tags:
            # get all histories
            histories = zeros((params['repetitions'], params['iterations']))
            skipped = []
            for i in range(params['repetitions']):
                logging.debug("Getting history over tag {} repetition {}".format(tag, i))
                try:
                    histories[i, :] = self.get_history(exp, i, tag)
                except ValueError:
                    h = self.get_history(exp, i, tag)
                    if len(h) == 0:
                        # history not existent, skip it
                        logging.warning('Exp: %s history %i for tag "%s" has length 0 (expected: %i). all other histories will be truncated.\n'%(exp, i, tag, params['iterations']))
                        skipped.append(i)
                    elif len(h) > params['iterations']:
                        # if history too long, crop it 
                        logging.warning('Expsuite: history %i has length %i (expected: %i). it will be truncated.\n'%(i, len(h), params['iterations']))
                        h = h[:params['iterations']]
                        histories[i,:] = h
                    elif len(h) < params['iterations']:
                        # if history too short, crop everything else
                        logging.warning('Exp: %s history %i for tag "%s" has length %i (expected: %i). all other histories will be truncated.\n'%(exp, i, tag, len(h), params['iterations']))
                        params['iterations'] = len(h)
                        histories = histories[:,:params['iterations']]
                        histories[i, :] = h

            # remove all rows that have bieen skipped
            logging.debug("removing indices {} from histories table {}".format(skipped,histories))
            try:
                histories = delete(histories, skipped, axis=0)
                params['repetitions'] -= len(skipped)
            except:
                pass
                
            # calculate result from each column with aggregation function
            aggregated = zeros(params['iterations'])
            for i in range(params['iterations']):
                aggregated[i] = aggregate(histories[:, i])
            
            # if only one tag is requested, return list immediately, otherwise append to dictionary
            if len(tags) == 1:
                return aggregated
            else:
                results[tag] = aggregated
            
        return results
        
        
    def haserror(self, params, rep):
        """ Helper function to identify exceptions on one experiment. """
        fullpath = os.path.join(params['path'], params['name'])
        logname = os.path.join(fullpath, '%i.log'%rep)
        if os.path.exists(logname):
            logfile = open(logname, 'r')
            lines = logfile.readlines()

            logfile.close()
            try: 
                if "exception:error" in lines[-1]:
                    return True
                else:
                    return False
            except IndexError: #if lines are empty
                return False
        else: 
            return False
    
    def browse(self): 
        """ go through all subfolders (starting at '.') and return information
            about the existing experiments. if the -B option is given, all 
            parameters are shown, -b only displays the most important ones.
            this function does *not* execute any experiments.
        """
        for d in self.get_exps('.'):
            params = self.get_params(d)
            name = params['name']
            basename = name.split('/')[0]
            # if -e option is used, only show requested experiments
            if self.options.experiments and basename not in self.options.experiments:
                continue
                
            fullpath = os.path.join(params['path'], name)
            
            # calculate progress
            prog = 0
            for i in range(params['repetitions']):
                prog += progress(params, i)
            prog /= params['repetitions']
            
            haserror = self.haserror(params, i)
            # if progress flag is set, only show the progress bars
            if self.options.progress:
                bar = "["
                bar += "="*int(prog/4)
                bar += " "*int(25-prog/4)
                bar += "]"
                if haserror:
                    bar += " *"
                print '%3i%% %27s %s'%(prog,bar,d)
                continue
            
            print '%16s %s'%('experiment', d)
                           
            try:
                minfile = min(
                    (os.path.join(dirname, filename)
                    for dirname, dirnames, filenames in os.walk(fullpath)
                    for filename in filenames
                    if filename.endswith(('.log', '.cfg'))),
                    key=lambda fn: os.stat(fn).st_mtime)
            
                maxfile = max(
                    (os.path.join(dirname, filename)
                    for dirname, dirnames, filenames in os.walk(fullpath)
                    for filename in filenames
                    if filename.endswith(('.log', '.cfg'))),
                    key=lambda fn: os.stat(fn).st_mtime)
            except ValueError:
                print '         started %s'%'not yet'
                
            else:      
                print '         started %s'%time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(os.stat(minfile).st_mtime))
                
                if haserror:
                    print '     *** crashed %s'%time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(os.stat(maxfile).st_mtime))
                else:
                    print '           ended %s'%time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(os.stat(maxfile).st_mtime))
            for k in ['repetitions', 'iterations']:
                print '%16s %s'%(k, params[k])   
            
            print '%16s %i%%'%('progress', prog)
            
            if self.options.browse_big:
                # more verbose output
                for p in [p for p in params if p not in ('repetitions', 'iterations', 'path', 'name')]:
                    print '%16s %s'%(p, params[p])
                    
            print                     
        
    def expand_param_list(self, paramlist):
        """ expands the parameters list according to one of these schemes:
            grid: every list item is combined with every other list item
            list: every n-th list item of parameter lists are combined 
        """
        # for one single experiment, still wrap it in list
        if type(paramlist) == types.DictType:
            paramlist = [paramlist]
        
        # get all options that are iteratable and build all combinations (grid) or tuples (list)
        iparamlist = []
        for params in paramlist:
            if ('experiment' in params and params['experiment'] == 'single'):
                iparamlist.append(params)
            else:
                iterparams = [p for p in params if hasattr(params[p], '__iter__') and not isinstance(params[p], dict)]
                if len(iterparams) > 0:
                    # write intermediate config file
                    self.mkdir(os.path.join(params['path'], params['name']))
                    self.write_config_file(params, os.path.join(params['path'], params['name']))

                    # create sub experiments (check if grid or list is requested)
                    if 'experiment' in params and params['experiment'] == 'list':
                        iterfunc = itertools.izip
                    elif ('experiment' not in params) or ('experiment' in params and params['experiment'] == 'grid'):
                        iterfunc = itertools.product
                    else:
                        raise SystemExit("unexpected value '%s' for parameter 'experiment'. Use 'grid', 'list' or 'single'."%params['experiment'])

                    for il in iterfunc(*[params[p] for p in iterparams]):
                        par = params.copy()
                        converted = str(zip(iterparams, map(convert_param_to_dirname, il)))
                        par['name'] = par['name'] + '/' + re.sub("[' \[\],()]", '', converted)
                        for i, ip in enumerate(iterparams):
                            par[ip] = il[i]
                        iparamlist.append(par)
                else:
                    iparamlist.append(params)

        return iparamlist

    
    def create_dir(self, params, delete=False):
        """ creates a subdirectory for the experiment, and deletes existing
            files, if the delete flag is true. then writes the current
            experiment.cfg file in the folder.
        """
        # create experiment path and subdir
        fullpath = os.path.join(params['path'], params['name'])
        self.mkdir(fullpath)

        # delete old histories if --del flag is active
        if delete:
            os.system('rm %s/*' % fullpath)
     
        # write a config file for this single exp. in the folder
        self.write_config_file(params, fullpath)
        
    def rerun_recursive(self):
        matched_filenames = []
        for root, dirnames, filenames in os.walk('.'):
            for filename in fnmatch.filter(filenames, "experiment.cfg"):
                matched_filenames.append(os.path.join(os.getcwd(), root,filename))            
        
        sys.stderr.write("Found nested filenames: \n{}\n\n".format("\n - ".join(matched_filenames)))
        for filename in matched_filenames:            
            self.options.config = filename
            self.options.rerun = self.options.rerun_recursive
            sys.stderr.write("\n*******************\nRunning {}\n\n".format(filename))
            try:
                self.parse_cfg()
            except IOError:
                sys.stderr.write("Could not read filename {}".format(filename))
                continue
            self.start()
            
    
    def start(self):
        """ starts the experiments as given in the config file. """     

        # if -b, -B or -p option is set, only show information, don't
        # start the experiments
        if self.options.browse or self.options.browse_big or self.options.progress:
            self.browse()
            raise SystemExit

        loglevel = logging.WARNING
        if self.options.debug:
            loglevel = logging.DEBUG
        logging.basicConfig(level=loglevel,
            format='%(asctime)s %(name)-12s %(levelname)-8s %(message)s',
            datefmt='%m-%d %H:%M')

        sys.setrecursionlimit(2000)
        
        # read main configuration file
        paramlist = []
        for exp in self.cfgparser.sections():
            if not self.options.experiments or exp in self.options.experiments:
                params = self.items_to_params(self.cfgparser.items(exp))
                params['name'] = exp
                paramlist.append(params)
                
        self.do_experiment(paramlist)
                
    
    def do_experiment(self, params):
        """ runs one experiment programatically and returns.
            params: either parameter dictionary (for one single experiment) or a list of parameter
            dictionaries (for several experiments).
        """
        paramlist = self.expand_param_list(params)
        
        # create directories, write config files
        for pl in paramlist:
            # check for required param keys
            if ('name' in pl) and ('iterations' in pl) and ('repetitions' in pl) and ('path' in pl):
               self.create_dir(pl, self.options.delete)
            else:
                print 'Error: parameter set does not contain all required keys: name, iterations, repetitions, path'
                return False
            
        # create experiment list 
        explist = []
            
        # expand paramlist for all repetitions and add self and rep number
        for p in paramlist:
            explist.extend(zip( [self]*p['repetitions'], [p]*p['repetitions'], xrange(p['repetitions']) ))
                
        # if only 1 process is required call each experiment seperately (no worker pool)
        if self.options.ncores == 1:
            for e in explist:
                mp_runrep(e)
        else:
            # create worker processes    
            pool = Pool(processes=self.options.ncores)
            pool.map(mp_runrep, explist)
        
        return True        
        
       
    def run_rep(self, params, rep):
        """ run a single repetition including directory creation, log files, etc. """
        name = params['name']
        fullpath = os.path.join(params['path'], params['name'])
        logname = os.path.join(fullpath, '%i.log'%rep)
        # check if repetition exists and has been completed
        restore = 0
        
        sys.stderr.write("Looking in path '{}'\n".format(fullpath))


        if not os.path.exists(logname):
            sys.stderr.write("log {} not found".format(logname))

        else:
            
            logfile = open(logname, 'r')
            lines = logfile.readlines()
            
            #throw away the line that reports the error
            try:
                if "exception:error" in lines[-1]:
                    lines = lines[:-1]
            except IndexError: #if lines is empty
                pass
            
            logfile.close()
            
            # if completed, continue loop
            if 'iterations' in params and len(lines) == params['iterations'] and not self.options.rerun:
                return False
            # if not completed, check if restore_state is supported
            if not self.restore_supported:
                # not supported, delete repetition and start over
                # print 'restore not supported, deleting %s' % logname
                os.remove(logname)
                restore = 0
            elif self.options.rerun and len(lines) < self.options.rerun:
                sys.stderr.write("Requested experiment has not reached this iteration")
                return False
            elif self.options.rerun and len(lines) >= self.options.rerun:
                logging.debug("Forced reruning after iteration %d\n", self.options.rerun)
                
                #backup existing logfile
                now = datetime.strftime(datetime.now(),"%Y-%m-%d_%H-%M")
                shutil.copy(logname, "{}.{}.bak".format(logname, now))
                
                #trim file to contain only repetitions we need
                lines = lines[0:self.options.rerun]
                print len(lines)
                logfile = open(logname, 'w')
                logfile.write("".join(lines))
                logfile.close()
                
                restore = self.options.rerun
            else:
                restore = len(lines)
                sys.stderr.write("Auto restoring after iteration %d\n"% restore)
                logging.debug("Auto restoring after iteration %d"% restore)
            
        self.reset(params, rep)
        
        if restore:
            logfile = open(logname, 'a')
            os.chdir(fullpath)
            self.restore_state(params, rep, restore)
        else:
            logfile = open(logname, 'w')
        
        # loop through iterations and call iterate
        for it in xrange(restore, params['iterations']):
            #set path for writing results of iteration
            os.chdir(fullpath)
            #initialize a local logger
            # create file handler which logs even debug messages
            loglevel = logging.DEBUG
            logging.basicConfig(level=loglevel,
            format='%(asctime)s %(name)-12s %(levelname)-8s %(message)s',
            datefmt='%m-%d %H:%M',
            filename='debuglog')

            try:
                dic = self.iterate(params, rep, it)
            except Exception as exc:
                #obtain the exception information
                trc = traceback.format_exc()
                self._print_exception(trc, exc, fullpath)
                
                #log the exception on the general rep log
                logfile.write("exception:error")
                
                #break the repeat loop (will lead to logfile.close())
                break
            
            if self.restore_supported:
                try:
                    self.save_state(params, rep, it)
                except Exception as exc:
                    #obtain the exception information, print them but don't break
                    trc = traceback.format_exc()
                    self._print_exception(trc, exc, fullpath)
                
            # replace all spaces in keys with underscores
            for k in dic:
                if ' ' in k:
                    newk = k.replace(' ', '_')
                    dic[newk] = dic[k]
                    del dic[k]
                    # issue warning but only once per key
                    if k not in self.key_warning_issued:
                        print "warning: key '%s' contained spaces and was renamed to '%s'"%(k, newk)    
                        self.key_warning_issued.append(k)
                
            # build string from dictionary
            outstr = ' '.join(map(lambda x: '%s:%s'%(x[0], str(x[1])), sorted(dic.items())))
            logfile.write("{}\n".format(outstr))
            logfile.flush()
        logfile.close()
    
    
    def _print_exception(self, trc, exc, fullpath):
        sys.stderr.write("\nSuite caught exception: {}\n".format(exc))
        sys.stderr.write("trace\n{}\n".format(trc))
        
        #create a one-time log file and put the exception information
        exception_logname = os.path.join(fullpath, datetime.now().strftime('exception-%Y_%m_%d__%H_%M_%S.stderr'))
        f = open(exception_logname, 'w')
        f.write("\nSuite caught exception: {}\n".format(exc))
        f.write("trace\n{}\n".format(trc))
        f.close()

    
    def reset(self, params, rep):
        """ needs to be implemented by subclass. """
        pass
    
    def iterate(self, params, rep, n):
        """ needs to be implemented by subclass. """
        ret = {'iteration':n, 'repetition':rep}
        return ret
    
    def save_state(self, params, rep, n):
        """ optionally can be implemented by subclass. """
        pass
        
    def restore_state(self, params, rep, n):
        """ if the experiment supports restarting within a repetition
            (on iteration level), load necessary stored state in this 
            function. Otherwise, restarting will be done on repetition 
            level, deleting all unfinished repetitions and restarting 
            the experiments.
        """
        pass
        
