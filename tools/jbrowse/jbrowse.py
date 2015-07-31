#!/usr/bin/env python
import os
import copy
import argparse
import subprocess
import hashlib
import tempfile
import json
import yaml
import logging
logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)

COLOR_FUNCTION_TEMPLATE = """
function(feature, variableName, glyphObject, track) {
    var score = {score};
    {opacity}
    return 'rgba({red}, {green}, {blue}, ' + opacity + ')';
}
"""

BLAST_OPACITY_MATH = """
var opacity = 0;
if(score == 0.0) {
    opacity = 1;
} else{
    opacity = (20 - Math.log10(score)) / 180;
}
"""

BREWER_COLOUR_IDX = 0
BREWER_COLOUR_SCHEMES = [
    (166, 206, 227),
    (31, 120, 180),
    (178, 223, 138),
    (51, 160, 44),
    (251, 154, 153),
    (227, 26, 28),
    (253, 191, 111),
    (255, 127, 0),
    (202, 178, 214),
    (106, 61, 154),
    (255, 255, 153),
    (177, 89, 40)
    # (228, 26, 28),
    # (55, 126, 184),
    # (77, 175, 74),
    # (152, 78, 163),
    # (255, 127, 0),
]

BREWER_DIVERGING_PALLETES = {
    'BrBg': ("#543005", "#003c30"),
    'PiYg': ("#8e0152", "#276419"),
    'PRGn': ("#40004b", "#00441b"),
    'PuOr': ("#7f3b08", "#2d004b"),
    'RdBu': ("#67001f", "#053061"),
    'RdGy': ("#67001f", "#1a1a1a"),
    'RdYlBu': ("#a50026", "#313695"),
    'RdYlGn': ("#a50026", "#006837"),
    'Spectral': ("#9e0142", "#5e4fa2"),
}


# score comes from feature._parent.get('score') or feature.get('score')
# Opacity math

TN_TABLE = {
    'gff3': '--gff',
    'gff': '--gff',
    'bed': '--bed',
    'genbank': '--gbk',
}

INSTALLED_TO = os.path.dirname(os.path.realpath(__file__))


class JbrowseConnector(object):

    def __init__(self, jbrowse, jbrowse_dir, outdir, genome):
        self.jbrowse = jbrowse
        self.jbrowse_dir = jbrowse_dir
        self.outdir = outdir
        self.genome_path = genome
        self.brewer_colour_idx = 0

        self.clone_jbrowse(self.jbrowse, self.outdir)
        self.process_genome()

    def subprocess_check_call(self, command):
        log.debug('cd %s && %s', self.jbrowse_dir, ' '.join(command))
        subprocess.check_call(command, cwd=self.jbrowse_dir)

    def _get_colours(self):
        r, g, b = BREWER_COLOUR_SCHEMES[self.brewer_colour_idx]
        self.brewer_colour_idx += 1
        return r, g, b

    def process_genome(self):
        self.subprocess_check_call(['perl', 'bin/prepare-refseqs.pl',
                                    '--fasta', self.genome_path])

    def _add_json(self, json_data):
        if len(json_data.keys()) == 0:
            return

        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.write(json.dumps(json_data))
        tmp.close()
        cmd = ['perl', 'bin/add-track-json.pl', tmp.name,
               os.path.join('data', 'trackList.json')]
        self.subprocess_check_call(cmd)
        os.unlink(tmp.name)

    def add_blastxml(self, data, trackData, **kwargs):
        gff3_unrebased = tempfile.NamedTemporaryFile(delete=False)
        cmd = ['python', os.path.join(INSTALLED_TO, 'blastxml_to_gapped_gff3.py'),
               '--trim_end', '--min_gap', str(kwargs['min_gap']), data]
        subprocess.check_call(cmd, cwd=self.jbrowse_dir, stdout=gff3_unrebased)
        gff3_unrebased.close()

        gff3_rebased = tempfile.NamedTemporaryFile(delete=False)
        cmd = ['python', os.path.join(INSTALLED_TO, 'gff3_rebase.py')]
        if kwargs['protein']:
            cmd.append('--protein2dna')
        cmd.extend([kwargs['parent'], gff3_unrebased.name])
        subprocess.check_call(cmd, cwd=self.jbrowse_dir, stdout=gff3_rebased)
        gff3_rebased.close()

        red, green, blue = self._get_colours()
        color_function = COLOR_FUNCTION_TEMPLATE.format({
            'score': "feature._parent.get('score')",
            'opacity': BLAST_OPACITY_MATH,
            'red': red,
            'green': green,
            'blue': blue,
        })

        clientConfig = trackData['style']
        clientConfig['color'] = color_function.replace('\n', '')
        config = {'glyph': 'JBrowse/View/FeatureGlyph/Segments'}
        if 'category' in kwargs:
            config['category'] = kwargs['category']

        cmd = ['perl', 'bin/flatfile-to-json.pl',
               '--gff', gff3_rebased.name,
               '--trackLabel', trackData['label'],
               '--key', trackData['key'],
               '--clientConfig', json.dumps(clientConfig),
               '--config', json.dumps(config),
               '--trackType', 'JBrowse/View/Track/CanvasFeatures'
               ]

        self.subprocess_check_call(cmd)
        os.unlink(gff3_rebased.name)
        os.unlink(gff3_unrebased.name)

    def _min_max_gff(self, gff_file):
        min_val = None
        max_val = None
        with open(gff_file, 'r') as handle:
            for line in handle:
                try:
                    value = float(line.split('\t')[5])
                    min_val = min(value, (min_val or value))
                    max_val = max(value, (max_val or value))

                    if value < min_val:
                        min_val = value

                    if value > max_val:
                        max_val = value
                except Exception:
                    pass
        return min_val, max_val

    def _parse_colours(self, track):
        # Wiggle tracks have a bicolor pallete
        clientConfig = {}
        if track['format'] == 'wiggle':
            if track['options']['style']['color_config'] == 'brewer':
                scheme = track['options']['style']['color']
                if scheme not in BREWER_DIVERGING_PALLETES:
                    raise Exception("Unknown pallete")

                pos_color, neg_color = BREWER_DIVERGING_PALLETES[scheme]
            else:
                pos_color = track['options']['style']['color_pos'].replace('__pd__', '#')
                neg_color = track['options']['style']['color_neg'].replace('__pd__', '#')

            clientConfig['pos_color'] = pos_color
            clientConfig['neg_color'] = neg_color
        else:
            # Other tracks either use "__auto__" or specify a colour
            if track['options']['style']['color'] == '__auto__':
                # Automatically generate the next brewer colour
                red, green, blue = self._get_colours()
                clientConfig['color'] = 'rgba({red}, {green}, {blue}, 1)' \
                    .format(red=red, green=green, blue=blue)
            else:
                clientConfig['color'] = track['options']['style']['color'].replace('__pd__', '#')
        return clientConfig

    def add_bigwig(self, data, trackData, **kwargs):
        dest = os.path.join('data', 'raw', os.path.basename(data))
        cmd = ['ln', data, dest]
        self.subprocess_check_call(cmd)

        trackData.update({
            "urlTemplate": os.path.join('..', dest),
            "storeClass": "JBrowse/Store/SeqFeature/BigWig",
            "type": "JBrowse/View/Track/Wiggle/Density",
        })

        if 'bicolor_pivot' not in trackData:
            trackData['bicolor_pivot'] = kwargs['style'].get('bicolor_pivot', 'zero')

        if 'type' in kwargs:
            trackData['type'] = kwargs['type']

        if 'min' in kwargs and 'max' in kwargs:
            trackData['min'] = kwargs['min']
            trackData['max'] = kwargs['max']
        else:
            trackData['autoscale'] = kwargs.get('autoscale', 'local')

        self._add_json(trackData)

    def add_bam(self, data, trackData, **kwargs):
        dest = os.path.join('data', 'raw', os.path.basename(data))
        cmd = ['ln', '-s', data, dest]
        self.subprocess_check_call(cmd)

        bai_source = kwargs['bam_index']
        cmd = ['ln', '-s', bai_source, dest + '.bai']
        self.subprocess_check_call(cmd)

        trackData.update({
            "urlTemplate": os.path.join('..', dest),
            "type": "JBrowse/View/Track/Alignments2",
            "storeClass": "JBrowse/Store/SeqFeature/BAM",
        })

        if 'category' in kwargs:
            trackData['category'] = kwargs['category']

        self._add_json(trackData)

        if kwargs.get('auto_snp', False):
            trackData2 = copy.copy(trackData)
            trackData2.update({
                "type": "JBrowse/View/Track/SNPCoverage",
                "key": trackData['key'] + " - SNPs/Coverage",
                "label": trackData['label']  + "_autosnp",
            })

            self._add_json(trackData)

    def add_vcf(self, data, clientConfig, **kwargs):
        dest = os.path.join('data', 'raw', os.path.basename(data))
        # ln?
        cmd = ['ln', '-s', data, dest]
        self.subprocess_check_call(cmd)
        cmd = ['bgzip', dest]
        self.subprocess_check_call(cmd)
        cmd = ['tabix', '-p', 'vcf', dest + '.gz']
        self.subprocess_check_call(cmd)

        trackData.update({
            "urlTemplate": os.path.join('..', dest + '.gz'),
            "type": "JBrowse/View/Track/HTMLVariants",
            "storeClass": "JBrowse/Store/SeqFeature/VCFTabix",
        })
        self._add_json(trackData)

    def add_features(self, data, format, trackData, **kwargs):
        cmd = [
            'perl', 'bin/flatfile-to-json.pl',
            TN_TABLE.get(format, 'gff'),
            data,
            '--trackLabel', trackData['label'],
            '--key', trackData['key']
        ]

        config = {}
        clientConfig = trackData['style']
        if 'category' in kwargs:
            config['category'] = kwargs['category']

        if kwargs.get('match', False):
            # Get min/max and build a scoring function since JBrowse doesn't
            min_val, max_val = self._min_max_gff(data)

            if min_val is not None and max_val is not None:
                MIN_MAX_OPACITY_MATH = """
                var opacity = (score - ${min}) * (1/(${max} - ${min}));
                """.format(**{
                    'max': max_val,
                    'min': min_val,
                })

                red, green, blue = self._get_colours()
                color_function = COLOR_FUNCTION_TEMPLATE.format({
                    'score': "feature.get('score')",
                    'opacity': MIN_MAX_OPACITY_MATH,
                    'red': red,
                    'green': green,
                    'blue': blue,
                })

                clientConfig['color'] = color_function.replace('\n', '')

            config['glyph'] = 'JBrowse/View/FeatureGlyph/Segments'

            cmd += ['--clientConfig', json.dumps(clientConfig),
                    '--trackType', 'JBrowse/View/Track/CanvasFeatures'
                    ]

        cmd.extend(['--config', json.dumps(config)])

        self.subprocess_check_call(cmd)

    def process_annotations(self, data, track):
        format = track['ext'][0]
        kwargs = track['options']

        # Base trackData section is pretty universally similar
        trackData = {
        }

        clientConfig = {
            'label': kwargs['style'].get('label', 'description'),
            'classname': kwargs['style'].get('className', 'feature'),
            'description': kwargs['style'].get('description', ''),
            'height': kwargs['style'].get('height', '100px')
        }

        # Colour parsing is complex due to different track types having
        # different colour options.
        clientConfig.update(self._parse_colours(track))

        # Load clientConfig into trackData
        trackData['style'] = clientConfig

        zipped_tracks = zip(track['files'], track['labels'])
        zipped_tracks.sort(key = lambda x: x[0])
        for i, (dataset, track_human_label) in enumerate(zipped_tracks):
            trackData['key'] = track_human_label
            trackData['label'] = hashlib.md5(dataset).hexdigest() + '_%s' % i

            # If a list of indices are available, set a variable with just the correct one.
            if 'bam_indexes' in track:
                track['bam_index'] = track['bam_indexes'][i]

            print "TrackData"
            import pprint; pprint.pprint(trackData)
            print "kwargs"
            import pprint; pprint.pprint(kwargs)

            if format in ('gff', 'gff3', 'bed'):
                self.add_features(dataset, format, trackData, **kwargs)
            elif format == 'bigwig':
                self.add_bigwig(dataset, trackData, **kwargs)
            elif format == 'bam':
                self.add_bam(dataset, trackData, **kwargs)
            elif format == 'blastxml':
                self.add_blastxml(dataset, trackData, **kwargs)
            elif format == 'vcf':
                self.add_vcf(dataset, trackData, **kwargs)

    def clone_jbrowse(self, jbrowse_dir, destination):
        # JBrowse seems to have included some bad symlinks, cp ignores bad symlinks
        # unlike copytree
        cmd = ['mkdir', '-p', destination]
        subprocess.check_call(cmd)
        cmd = ['cp', '-r', jbrowse_dir, destination]
        subprocess.check_call(cmd)
        cmd = ['mkdir', '-p', os.path.join(destination, 'JBrowse-1.11.6',
                                           'data', 'raw')]
        subprocess.check_call(cmd)

        # http://unix.stackexchange.com/a/38691/22785
        # JBrowse releases come with some broken symlinks
        cmd = ['find', destination, '-type', 'l', '-xtype', 'l', '-exec', 'rm', "'{}'", '+']
        subprocess.check_call(cmd)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="", epilog="")
    parser.add_argument('genome', type=file, help='Input genome file')
    parser.add_argument('yaml', type=file, help='Track Configuration')

    parser.add_argument('--jbrowse', help='Folder containing a jbrowse release')
    parser.add_argument('--outdir', help='Output directory', default='out')
    args = parser.parse_args()

    jc = JbrowseConnector(
        jbrowse=args.jbrowse,
        jbrowse_dir=os.path.join(args.outdir, 'JBrowse-1.11.6'),
        outdir=args.outdir,
        genome=os.path.realpath(args.genome.name),
    )

    track_data = yaml.load(args.yaml)
    for track in track_data:
        paths = [os.path.realpath(x) for x in track['files']]
        extra = track.get('options', {})

        for possible_partial_path in ('bam_indexes', 'parent'):
            if possible_partial_path in extra:
                if isinstance(extra[possible_partial_path], list):
                    extra[possible_partial_path] = [os.path.realpath(x) for x in extra[possible_partial_path]]
                else:
                    extra[possible_partial_path] = os.path.realpath(extra[possible_partial_path])
        extra['category'] = track.get('category', 'Default')

        # Push modified options back into the track config
        track['options'] = extra
        # More data was needed in process_annotations so it becomes a little
        # bit less clean.
        jc.process_annotations(paths, track)

    print """
    <html>
        <body>
        <script type="text/javascript">
            window.location=JBrowse-1.11.6/index.html
        </script>
        <a href="JBrowse-1.11.6/index.html">Go to JBrowse</a>
        <p>Please note that JBrowse functions best on production Galaxy
        instances, under X-Sendfile. The paste server used in development
        instances has issues serving the volumes of data regularly involved in
        JBrowse, and handling subrange requests needed for BAM files.</p>
        </body>
    </html>
    """
