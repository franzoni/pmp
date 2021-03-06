from pyelasticsearch import ElasticSearch
import config
import copy
import json
import math
import time


class GetChain():

    def __init__(self):
        self.countDummy = 0
        self.es = ElasticSearch(config.DATABASE_URL)
        self.overflow = 1000000

    def fakeId(self):
        self.countDummy += 1
        return 'X'*(5-len('%d' % (self.countDummy)))+'%d' % (self.countDummy)

    def __createDummyRequest(self, req, memberOfCampaign, status='upcoming',
                             total=None):
        fake_r = {}
        fake_r['status'] = status
        fake_r['member_of_campaign'] = memberOfCampaign
        for member in ['pwg', 'priority', 'total_events', 'time_event']:
            fake_r[member] = req[member]
        if total is not None:
            fake_r['total_events'] = total
        fake_r['prepid'] = '-'.join([req['pwg'],
                                     memberOfCampaign, self.fakeId()])
        fake_r['cloned_from'] = req['prepid']
        return fake_r

    def get(self, campaign):
        arg_list = campaign.split(',')
        # Get all chained campaigns which contain selected CAMPAIGN
        # reduction to only cc
        while True:
            again = False
            for arg in arg_list:
                if not arg.startswith('chain'):
                    # this is a flow, or a campaign: not matter for the query
                    ccs = [s['_source'] for s in
                           self.es.search(('campaigns:%s' % arg),
                                          index='chained_campaigns',
                                          size=self.overflow)['hits']['hits']]
                    arg_list.extend(map(lambda cc: cc['prepid'], ccs))
                    arg_list.remove(arg)
                    again = True
                    break
            if not again:
                break
        #  arg_list contains only chained campaigns
        steps = []  # what are the successive campaigns
        all_cr = []  # what are the chained requests to look at
        all_cc = {}
        # unique it
        arg_list = list(set(arg_list))
        # collect all crs
        for a_cc in arg_list:
            try:
                mcm_cc = self.es.get('chained_campaigns',
                                     'chain_campaign', a_cc)['_source']
            except Exception:
                # try to see if that's a flow
                return '%s does not exists' % (a_cc)
            all_cc[a_cc] = mcm_cc  # keep it in mind
            all_cr.extend([s['_source'] for s in
                           self.es.search(('member_of_campaign:%s' % a_cc),
                                          index='chained_requests',
                                          size=self.overflow)['hits']['hits']])
            these_steps = map(lambda s: s[0], mcm_cc['campaigns'])
            if len(steps) == 0:
                steps = these_steps
            else:
                # concatenate to existing steps
                # add possible steps at the beginning
                connection = 0
                while not steps[connection] in these_steps:
                    connection += 1
                new_start = these_steps.index(steps[connection])
                if new_start != 0:
                    # they do not start at the same campaign
                    for where in range(new_start):
                        steps.insert(where, these_steps[where])
                # verify strict overlapping
                # ==> does not function properly and limits the flexibility
                for check in range(new_start, len(these_steps)):
                    if these_steps[check] not in steps:
                        steps.append(these_steps[check])
        # preload all requests !!!
        all_requests = {}
        for step in steps:
            for r in [s['_source'] for s in
                      self.es.search(('member_of_campaign:%s' % step),
                                     index='requests',
                                     size=self.overflow)['hits']['hits']]:
                all_requests[r['prepid']] = r
        # avoid double counting
        already_counted = set()
        # the list of requests to be emitted to d3js
        list_of_request_for_ramunas = []
        for cr in all_cr:
            upcoming = 0
            if len(cr['chain']) == 0:
                # crap data
                continue
            stop_at = cr['step']
            stop_at = len(cr['chain'])-1
            for (r_i, r) in enumerate(cr['chain']):
                if r_i > stop_at:
                    # this is a reserved request, will count as upcoming later
                    continue
                mcm_r = all_requests[r]
                upcoming = mcm_r['total_events']
                if r in already_counted:
                    continue
                else:
                    already_counted.add(r)

                # add it to emit
                def pop(mcm_r):
                    for member in mcm_r.keys():
                        if member not in ['prepid', 'pwg', 'priority',
                                          'total_events', 'status',
                                          'member_of_campaign', 'time_event']:
                            mcm_r.pop(member)
                    return mcm_r

                if mcm_r['status'] == 'submitted':
                    mcm_r_fake_done = copy.deepcopy(mcm_r)
                    mcm_r_fake_done['status'] = 'done'
                    mcm_r_fake_done['total_events'] = mcm_r['completed_events']
                    mcm_r_fake_subm = copy.deepcopy(mcm_r)
                    mcm_r_fake_subm['total_events'] = max(
                        [0, mcm_r['total_events'] - mcm_r['completed_events']])
                    list_of_request_for_ramunas.append(pop(mcm_r_fake_subm))
                    list_of_request_for_ramunas.append(pop(mcm_r_fake_done))
                else:
                    list_of_request_for_ramunas.append(pop(mcm_r))
            for noyet in all_cc[cr[
                    'member_of_campaign']]['campaigns'][stop_at+1:]:
                # create a fake request with the proper member of campaign
                processing_r = all_requests[cr['chain'][stop_at]]
                fake_one = self.__createDummyRequest(processing_r, noyet[0],
                                                     total=upcoming)
                list_of_request_for_ramunas.append(fake_one)
        return json.dumps({"results": list_of_request_for_ramunas})


class GetCampaign():

    def __init__(self):
        self.es = ElasticSearch(config.DATABASE_URL)
        self.overflow = 1000000

    def get(self, campaign):
        if campaign == 'all':
            campaign = '*'
        return json.dumps(
            {"results": [s['_source'] for s in
                         self.es.search(('member_of_campaign:%s' % campaign),
                                        index='requests',
                                        size=self.overflow)['hits']['hits']]})


class GetLifetime():

    def __init__(self):
        self.es = ElasticSearch(config.DATABASE_URL)
        # normally es will crop results
        # and a million rows is more than we have in db
        self.overflow = 1000000

    def db_query(self, input):
        """
        Query DB and return array of raw documents
        """
        iterable = []
        try:
            # check if the input is a campaign
            req_arr = [s['_source'] for s in
                       self.es.search(('member_of_campaign:%s' % input),
                                      index='requests',
                                      size=self.overflow)['hits']['hits']]

            for r in req_arr:
                res = ([s['name'] for s in
                        self.es.get('requests', 'request',
                                    r['prepid'])['_source']['reqmgr_name']])
                for e in res:
                    iterable.append(e)
        except:
            pass

        if not len(iterable):
            try:
                # check if the input is a request
                iterable = [s['name'] for s in
                            self.es.get('requests', 'request',
                                        input)['_source']['reqmgr_name']]
            except:
                # input can be a reqmgr_name
                iterable = [input]

        for i in iterable:
            try:
                yield self.es.get('stats', 'stats', i)['_source']
            except:
                yield None

    def rm_useless(self, arr):
        r = []
        prev = {'a': -1, 'e': -1, 'x': -1}
        for a in arr:
            if a['a'] != prev['a'] or a['e'] != prev['e'] or a['x'] != prev['x']:
                r.append(a)
                prev = a
        return r

    def prepare_response(self, query):
        #print "Start"
        #prev = int(round(time.time() * 1000))
        #print prev
        r = []

        # Process the db documents
        for d in self.db_query(query):

            if d is None:
                continue

            response = {}
            response['campaign'] = d['pdmv_campaign']
            response['data'] = []
            response['input'] = query
            response['priority'] = d['pdmv_priority']
            response['pwg'] = '#HaveToQueryRequest'
            response['request'] = d['pdmv_prep_id']
            response['status'] = '#HaveToQueryRequest'
            response['title'] = d['pdmv_prep_id'] + d['pdmv_dataset_name']

            if 'pdmv_monitor_history' in d:
                for record in d['pdmv_monitor_history']:
                    if len(record['pdmv_monitor_time']):
                        data = {}
                        data['a'] = record['pdmv_evts_in_DAS'] + record['pdmv_open_evts_in_DAS']
                        data['e'] = record['pdmv_evts_in_DAS']
                        data['t'] = time.mktime(time.strptime(record['pdmv_monitor_time']))*1000
                        data['x'] = d['pdmv_expected_events']
                        response['data'].append(data)
            r.append(response)
        
        #print "Data prepared"
        #print int(round(time.time() * 1000)) - prev
        #prev = int(round(time.time() * 1000))

        # Step 1: Get accumulated requests
        tmp = {}
        for x in r:
            s = x['request']
            try:
                tmp[s] += x['data']
            except KeyError:
                tmp[s] = x['data']
            tmp[s] = self.rm_useless(tmp[s])

        #for name in tmp:
        #    tmp[name] = sorted(tmp[name], key=lambda e: e['t'])

        #print "Accum request"
        #print int(round(time.time() * 1000)) - prev
        #prev = int(round(time.time() * 1000))


        # Step 2: Get and sort timestamps
        times = []
        for t in tmp:
            times += (x['t'] for x in tmp[t])
        times = sorted(set(times))
        #print "Sorted times"
        #print int(round(time.time() * 1000)) - prev
        #prev = int(round(time.time() * 1000))

        '''
        Step 3 & Step 4
        data = []
        for t in times:
            dummy = {'a':0, 'e':0, 'x':0, 't': t}
            for name in tmp:
                pre = {'a':0, 'e':0, 'x':0}
                for i in xrange(len(tmp[name])):
                    if tmp[name][i]['t'] == t:
                        dummy['a'] += tmp[name][i]['a']
                        dummy['e'] += tmp[name][i]['e']
                        dummy['x'] += tmp[name][i]['x']
                        break
                    elif tmp[name][i]['t'] > t:
                        dummy['a'] += pre['a']
                        dummy['e'] += pre['e']
                        dummy['x'] += pre['x']
                        break
                    elif tmp[name][i]['t'] < t:
                        pre = tmp[name][i]
            data.append(dummy)
        '''

        # Step 3: Create dummy points for each request
        tmp2 = {}
        for t in tmp:
            nxw = []
            cur_index = 0
            dummy = {'a':0, 'e':0, 'x':0}
            listed = sorted(tmp[t], key=lambda e: e['t'])
            for a in times:
                if cur_index < len(listed) and a == listed[cur_index]['t']:
                    dummy = listed[cur_index]
                    cur_index += 1
                dummy['t'] = a
                nxw.append(dummy)
            tmp2[t] = nxw
        #print "Dummy points"
        #print int(round(time.time() * 1000)) - prev
        #prev = int(round(time.time() * 1000))

        # get only 1000 points
        skiper = len(times) / 20

        # Step 4: Generating data points
        data = []
        i = 0

        for (x, t) in enumerate(times):
            if i < skiper and x < len(times) - 1 and x != 0:
                i += 1
            else:
                i = 0
                d = {'a': 0, 'e':0, 't': t, 'x': 0}
                for m in tmp2:
                    d['a'] += tmp2[m][x]['a']
                    d['e'] += tmp2[m][x]['e']
                    d['x'] += tmp2[m][x]['x']
                data.append(d)
            
        #print "Data points"
        #print int(round(time.time() * 1000)) - prev
        print len(data)
        return data

    def get(self, query):
        return json.dumps({"results": self.prepare_response(query)})


class GetSuggestions():

    def __init__(self, typeof):
        self.es = ElasticSearch(config.DATABASE_URL)
        self.overflow = 20
        self.lifetime = (typeof == 'lifetime')
        self.on = (typeof == 'true')

    def get(self, query):

        searchable = query.replace('-', '\-')

        if self.lifetime:

            if '-' in query:
                search_string = ('prepid:%s' % searchable)
                search_stats = ('pdmv_request_name:%s' % searchable)
            else:
                search_string = ('prepid:*%s*' % searchable)
                search_stats = ('pdmv_request_name:*%s*' % searchable)

            campa = [s['_id'] for s in
                     self.es.search(search_string, index='campaigns',
                                    size=self.overflow)['hits']['hits']]

            reque = [s['_id'] for s in
                     self.es.search(search_string, index='requests',
                                    size=self.overflow)['hits']['hits']]

            stats = [s['_id'] for s in
                     self.es.search(search_stats, index='stats',
                                    size=self.overflow)['hits']['hits']]

            return json.dumps({'results': campa + reque + stats})

        else:
            if '-' in query:
                search_string = ('prepid:%s' % searchable)
            else:
                search_string = ('prepid:*%s*' % searchable)

            if self.on:
                return json.dumps(
                    {"results": [s['_id'] for s in
                                 self.es.search(search_string,
                                                index="chained_campaigns",
                                                size=self.overflow)
                                 ['hits']['hits']]
                     + [s['_id'] for s in
                        self.es.search(search_string, index="chained_requests",
                                       size=self.overflow)['hits']['hits']]})
            else:
                return json.dumps(
                    {"results": [s['_id'] for s in
                                 self.es.search(self.search_string,
                                                index="campaigns",
                                                size=self.overflow)
                                 ['hits']['hits']]})
