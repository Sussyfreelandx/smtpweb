template_id = db.Column(
    db.Integer,
    db.ForeignKey('email_template.id', ondelete='SET NULL'),
    nullable=True
)

template = db.relationship(
    'EmailTemplate',
    backref=db.backref('campaigns', passive_deletes=True),
    passive_deletes=True
)
